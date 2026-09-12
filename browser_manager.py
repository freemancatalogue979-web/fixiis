"""
Browser Manager - Browser creation, pooling, and lifecycle management
Handles real Chrome browser with stealth mode, persistent profiles, and resource pooling
With enhanced profile management for per-user session persistence
Cross-platform support: Windows (visible) and Linux (Xvfb headless)
"""

import asyncio
import os

# Logical-pixel policy: 1 CSS pixel == 1 raster pixel for every session,
# so the CDP screencast frame IS exactly the page content (no browser-area
# padding, no scaled surfaces).

# Chromium enforces a ~500 CSS px MINIMUM window width on any headed
# window (real display or Xvfb) - it silently widens --window-size below
# that. Session layout viewports are floored to it so a page can never
# sit inside a surface wider than itself (that gap painted the white
# strip). 500 still reads as a phone width to every responsive breakpoint
# that matters (>480 layouts stay mobile).
MIN_VIEWPORT_WIDTH = 500

# Window chrome (X11 title bar + tab strip + toolbar) on the headed/Xvfb
# build. --window-size sets the OUTER height; the inner page surface comes
# out WINDOW_CHROME_HEIGHT shorter (measured: 844 outer -> 687 inner).
# Manually-sized launch paths inflate by this so inner == requested
# layout height and nothing is clipped off the bottom.
WINDOW_CHROME_HEIGHT = 157

import sys
import time
import shutil
import json
import hashlib
import socket
import subprocess
import urllib.request
import threading
import tempfile
from typing import Dict, Optional, Any, List
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Profile metadata/cookie files are shared by BrowserManager instances even
# though each runtime browser is isolated.  Key the short synchronous file
# critical sections by canonical parent profile so concurrent sessions cannot
# truncate or overwrite one another's durable state.
_PROFILE_IO_LOCKS: Dict[str, threading.RLock] = {}
_PROFILE_IO_LOCKS_GUARD = threading.RLock()
_PROFILE_ACTIVE_SESSIONS: Dict[str, set] = {}
_PROFILE_ACTIVE_GUARD = threading.RLock()

def register_profile_session(user_id: str, session_id: str) -> None:
    if not user_id or not session_id:
        return
    with _PROFILE_ACTIVE_GUARD:
        _PROFILE_ACTIVE_SESSIONS.setdefault(str(user_id), set()).add(str(session_id))

def unregister_profile_session(user_id: str, session_id: str) -> None:
    if not user_id or not session_id:
        return
    with _PROFILE_ACTIVE_GUARD:
        sessions = _PROFILE_ACTIVE_SESSIONS.get(str(user_id))
        if not sessions:
            return
        sessions.discard(str(session_id))
        if not sessions:
            _PROFILE_ACTIVE_SESSIONS.pop(str(user_id), None)

def profile_has_active_session(user_id: str, exclude_session_id: Optional[str] = None) -> bool:
    with _PROFILE_ACTIVE_GUARD:
        sessions = _PROFILE_ACTIVE_SESSIONS.get(str(user_id), set())
        if exclude_session_id is None:
            return bool(sessions)
        return any(sid != str(exclude_session_id) for sid in sessions)

def _profile_io_lock(profile_path: Path) -> threading.RLock:
    key = str(profile_path.resolve())
    with _PROFILE_IO_LOCKS_GUARD:
        lock = _PROFILE_IO_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROFILE_IO_LOCKS[key] = lock
        return lock

def _atomic_json_write(path: Path, value: Any) -> None:
    """Write one profile record without exposing a partially-written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except Exception:
            pass

# Mobile stealth toggle - set to False to disable all mobile stealth functionality
ENABLE_MOBILE_STEALTH = True

# Shared Android Chrome identity used whenever an iPhone/iPad client is
# represented by the remote browser.
ANDROID_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 15; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Mobile Safari/537.36"
)

# Try to import CDP for direct Chrome connection (replaces Playwright)
try:
    import asyncio_dgram
    CDP_AVAILABLE = True
except ImportError:
    CDP_AVAILABLE = False
    logger.warning("CDP libraries not available - using Playwright fallback")

# Try to import playwright-stealth for enhanced evasion
try:
    from playwright_stealth import Stealth
    PLAYWRIGHT_STEALTH_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_STEALTH_AVAILABLE = False
    logger.warning("playwright-stealth not available - using custom evasion scripts")


def is_windows() -> bool:
    """Check if running on Windows"""
    return sys.platform.startswith('win') or sys.platform == 'cygwin'


def normalize_path_for_playwright(path: str) -> str:
    """Convert Windows paths to forward slashes for Playwright compatibility"""
    if is_windows():
        return str(Path(path)).replace('\\', '/')
    return str(Path(path))


# ============================================================
# Device presets - unified table (mobile_devices.json)
# ============================================================

# Embedded fallback, used ONLY if mobile_devices.json is missing/corrupt.
# Mirrors the previous hardcoded tables (FingerprintManager + DirectChromeLauncher).
_EMBEDDED_MOBILE_DEVICES = {
    'iphone_14_pro': {
        'user_agent': ANDROID_MOBILE_USER_AGENT,
        'viewport': {'width': 393, 'height': 852}, 'device_scale_factor': 1.0,
        'is_mobile': True, 'has_touch': True, 'platform': 'Linux; Android 15',
        'cpu_cores': 8, 'memory': 8, 'max_touch_points': 5,
        'webgl_vendor': 'Google Inc.', 'webgl_renderer': 'Mali G78',
        'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        'screen_height': 800},
    'iphone_14': {
        'user_agent': ANDROID_MOBILE_USER_AGENT,
        'viewport': {'width': 390, 'height': 844}, 'device_scale_factor': 1.0,
        'is_mobile': True, 'has_touch': True, 'platform': 'Linux; Android 15',
        'cpu_cores': 8, 'memory': 8, 'max_touch_points': 5,
        'webgl_vendor': 'Google Inc.', 'webgl_renderer': 'Mali G78',
        'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        'screen_height': 792},
    'galaxy_s23': {
        'user_agent': 'Mozilla/5.0 (Linux; Android 15; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36',
        'viewport': {'width': 384, 'height': 854}, 'device_scale_factor': 1.0,
        'is_mobile': True, 'has_touch': True, 'platform': 'Linux; Android 15',
        'cpu_cores': 8, 'memory': 8, 'max_touch_points': 5,
        'webgl_vendor': 'ARM', 'webgl_renderer': 'Mali G78',
        'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        'screen_height': 800},
    'pixel_7': {
        'user_agent': 'Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36',
        'viewport': {'width': 412, 'height': 915}, 'device_scale_factor': 1.0,
        'is_mobile': True, 'has_touch': True, 'platform': 'Linux; Android 15',
        'cpu_cores': 8, 'memory': 8, 'max_touch_points': 5,
        'webgl_vendor': 'Google Inc.', 'webgl_renderer': 'Mali G78',
        'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        'screen_height': 860},
    'pixel_5': {
        'user_agent': 'Mozilla/5.0 (Linux; Android 15; Pixel 5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36',
        'viewport': {'width': 393, 'height': 851}, 'device_scale_factor': 1.0,
        'is_mobile': True, 'has_touch': True, 'platform': 'Linux; Android 15',
        'cpu_cores': 8, 'memory': 8, 'max_touch_points': 5,
        'webgl_vendor': 'Google Inc.', 'webgl_renderer': 'Mali G78',
        'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        'screen_height': 800},
    'desktop': {
        'user_agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        'viewport': {'width': 1920, 'height': 1080}, 'device_scale_factor': 1.0,
        'is_mobile': False, 'has_touch': False},
    'windows_11_chrome': {
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        'viewport': {'width': 1920, 'height': 1080}, 'device_scale_factor': 1.0,
        'is_mobile': False, 'has_touch': False, 'platform': 'Win32',
        'oscpu': 'Windows NT 10.0; Win64; x64', 'cpu_cores': 8, 'memory': 16,
        'max_touch_points': 0,
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0',
        'fonts': ['Segoe UI', 'Arial', 'Times New Roman', 'Calibri', 'Consolas']},
    'windows_11_edge': {
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0',
        'viewport': {'width': 1920, 'height': 1080}, 'device_scale_factor': 1.0,
        'is_mobile': False, 'has_touch': False, 'platform': 'Win32',
        'oscpu': 'Windows NT 10.0; Win64; x64', 'cpu_cores': 8, 'memory': 16,
        'max_touch_points': 0,
        'webgl_vendor': 'Google Inc. (NVIDIA)',
        'webgl_renderer': 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0',
        'fonts': ['Segoe UI', 'Arial', 'Times New Roman', 'Calibri', 'Consolas']},
    'mac_chrome': {
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
        'viewport': {'width': 1920, "height": 1080}, 'device_scale_factor': 1.0,
        'is_mobile': False, 'has_touch': False, 'platform': 'MacIntel',
        'oscpu': 'Intel Mac OS X 10_15_7', 'cpu_cores': 8, 'memory': 16,
        'max_touch_points': 0,
        'webgl_vendor': 'Google Inc. (Apple)', 'webgl_renderer': 'Apple GPU',
        'fonts': ['SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'Arial', 'Times New Roman']},
}


def load_mobile_devices() -> Dict[str, Dict]:
    """
    Load the unified device preset table (single source of truth).

    Reads mobile_devices.json next to this file. Falls back to the embedded
    table (previous hardcoded values) if the file is missing or corrupt.
    """
    try:
        path = Path(__file__).resolve().parent / 'mobile_devices.json'
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        devices = {k: v for k, v in data.items() if not k.startswith('_')}
        if devices:
            return devices
        raise ValueError('empty device table')
    except Exception as e:
        logger.warning(f"[Devices] mobile_devices.json unavailable ({e}) - using embedded fallback")
        return dict(_EMBEDDED_MOBILE_DEVICES)


_MOBILE_DEVICES_TABLE = load_mobile_devices()

# iPhone/iPad clients are intentionally represented as Android Chrome in the
# remote browser.  Keep one exact UA across the SeleniumBase and Playwright
# launch paths so the HTTP header, CDP override, and JS fingerprint agree.
def is_apple_mobile_user_agent(user_agent: Optional[str]) -> bool:
    """Return True for iPhone/iPad/iPod UAs, including iPadOS desktop UAs."""
    if not isinstance(user_agent, str):
        return False
    ua = user_agent.lower()
    if any(token in ua for token in ("iphone", "ipad", "ipod")):
        return True
    # iPadOS can advertise itself as Macintosh while retaining the Mobile/
    # token.  Do not classify ordinary desktop Mac Chrome as an iPad.
    return "macintosh" in ua and "mobile/" in ua and "safari" in ua


def normalize_mobile_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """Map Apple mobile UAs to the project-wide Android Chrome UA."""
    if is_apple_mobile_user_agent(user_agent):
        return ANDROID_MOBILE_USER_AGENT
    return user_agent


def match_device_preset(user_agent: str, is_mobile: bool) -> Optional[Dict]:
    """
    Match a device preset to a client user agent.

    Exact UA match first, then the platform family. Returns None when nothing
    fits (callers keep their default pipeline values).
    """
    user_agent = normalize_mobile_user_agent(user_agent)
    if not user_agent:
        return None
    if not is_mobile:
        for key in ('windows_11_chrome', 'mac_chrome', 'desktop'):
            dev = _MOBILE_DEVICES_TABLE.get(key)
            if dev and dev.get('user_agent') == user_agent:
                return dev
        return None
    for dev in _MOBILE_DEVICES_TABLE.values():
        if dev.get('is_mobile') and dev.get('user_agent') == user_agent:
            return dev
    if 'iPhone' in user_agent or 'iPad' in user_agent:
        return _MOBILE_DEVICES_TABLE.get('iphone_14_pro')
    if 'Android' in user_agent:
        return _MOBILE_DEVICES_TABLE.get('pixel_7')
    return None




class PlatformRuntime:
    """
    Decides HOW the browser launches on this machine.

    Real users browse in a real browser window. Headless Chrome is the
    single strongest bot signal (outerHeight == innerHeight, no window
    frame, no screen coordinates), so the priority is:

        1. 'visible'  - a real display exists (Windows desktop/RDP,
                        Linux X11/Wayland, macOS). Window shown normally.
        2. 'xvfb'     - headless Linux server with Xvfb installed:
                        virtual display + HEADED Chrome. To the browser
                        this is indistinguishable from a real monitor.
        3. 'headless' - last resort only (Android/Termux where no X server
                        is possible, or no display and no Xvfb installed).

    All launch paths (direct CDP, Playwright create_browser,
    create_browser_simple) MUST use this single decision.
    """

    MODE_VISIBLE = 'visible'
    MODE_XVFB = 'xvfb'
    MODE_HEADLESS = 'headless'

    @staticmethod
    def is_termux() -> bool:
        """True when running inside Termux on Android."""
        if sys.platform == 'linux':
            prefix = getattr(sys, 'prefix', '') or ''
            return ('/com.termux/' in prefix
                    or bool(os.environ.get('TERMUX_VERSION'))
                    or bool(os.environ.get('__ANDROID_DATA__')))
        return False

    @classmethod
    def os_name(cls) -> str:
        """Normalized OS name: windows | linux | macos | android_termux | other"""
        if cls.is_termux():
            return 'android_termux'
        if sys.platform.startswith('win') or sys.platform == 'cygwin':
            return 'windows'
        if sys.platform == 'darwin':
            return 'macos'
        if sys.platform.startswith('linux'):
            return 'linux'
        return 'other'

    @classmethod
    def has_display(cls) -> bool:
        """True if a real display is available on this machine."""
        os_name = cls.os_name()
        if os_name == 'windows':
            return True  # interactive desktop or RDP session
        if os_name == 'macos':
            # CI runners (GitHub Actions etc.) have no window server
            return not (os.environ.get('CI') or os.environ.get('CONTINUOUS_INTEGRATION'))
        if os_name == 'linux':
            return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
        return False

    @classmethod
    def resolve(cls, force_headless: bool = False) -> Dict:
        """
        Resolve the launch mode for this machine (does NOT start Xvfb).
        Returns {'mode': 'visible'|'xvfb'|'headless', 'reason': str}
        """
        if force_headless:
            return {'mode': cls.MODE_HEADLESS, 'reason': 'forced (config headless=True)'}

        os_name = cls.os_name()

        if os_name == 'android_termux':
            # No X server is possible on Android - headless is the only option
            return {'mode': cls.MODE_HEADLESS, 'reason': 'Android/Termux (no X server)'}

        if cls.has_display():
            return {'mode': cls.MODE_VISIBLE, 'reason': f'real display available ({os_name})'}

        # No display. On Linux, Xvfb is the fix: virtual monitor + headed Chrome.
        if os_name == 'linux':
            xvfb = get_xvfb_manager()
            xvfb.ensure_checked()
            if xvfb.xvfb_available:
                return {'mode': cls.MODE_XVFB,
                        'reason': 'headless Linux, Xvfb available (headed on virtual display)'}
            logger.warning(
                "[PlatformRuntime] No display and Xvfb not installed - falling back to "
                "HEADLESS mode (strongest bot signal). Fix: apt-get install xvfb")
            return {'mode': cls.MODE_HEADLESS, 'reason': 'headless Linux, Xvfb NOT installed'}

        return {'mode': cls.MODE_HEADLESS, 'reason': f'no display ({os_name})'}


class BrowserDialogHandler:
    """
    Handles native browser dialogs (alert, confirm, prompt, beforeunload)
    by capturing them on the server and forwarding to client for user interaction.
    FIXED: Added max pending dialogs limit, automatic cleanup, and timeout enforcement.
    """
    
    def __init__(self):
        self.pending_dialogs: Dict[str, asyncio.Future] = {}
        self.dialog_callback = None  # Will be set to send to client
        self._lock = asyncio.Lock()
        # FIX: Limit pending dialogs to prevent memory issues
        self.max_pending_dialogs = 50
        self.dialog_timeout = 30.0  # seconds
        self._cleanup_task = None
        self._running = True
        # Track dialog creation time for timeout monitoring
        self._dialog_timestamps: Dict[str, float] = {}
    
    async def start_cleanup_task(self):
        """Start background task to clean up stale dialogs"""
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                await self._cleanup_stale_dialogs()
            except Exception as e:
                logger.error(f"DialogHandler cleanup error: {e}")
    
    async def stop_cleanup_task(self):
        """Stop the cleanup task"""
        self._running = False
    
    async def _cleanup_stale_dialogs(self):
        """Clean up dialogs that have been waiting too long"""
        async with self._lock:
            current_time = time.time()
            stale_dialogs = []
            
            for dialog_id, timestamp in list(self._dialog_timestamps.items()):
                if current_time - timestamp > self.dialog_timeout:
                    stale_dialogs.append(dialog_id)
            
            for dialog_id in stale_dialogs:
                if dialog_id in self.pending_dialogs:
                    future = self.pending_dialogs.pop(dialog_id, None)
                    if future and not future.done():
                        future.set_result(None)  # Resolve with None (dismiss)
                    self._dialog_timestamps.pop(dialog_id, None)
                    logger.warning(f"DialogHandler: Force-resolved stale dialog {dialog_id}")
    
    def set_dialog_callback(self, callback):
        """Set callback function to send dialogs to client"""
        self.dialog_callback = callback
    
    async def handle_dialog(self, dialog, session_id: str) -> str:
        """Handle a browser dialog - forward to client for response"""
        
        # FIX: Check if we're at capacity before creating new dialog
        async with self._lock:
            if len(self.pending_dialogs) >= self.max_pending_dialogs:
                logger.warning(f"DialogHandler: At max capacity ({self.max_pending_dialogs}), auto-dismissing dialog")
                await dialog.dismiss()
                return "dismissed"
        
        # Create a future that will hold the client's response
        loop = asyncio.get_event_loop()
        response_future = loop.create_future()
        
        # Store the future with a unique dialog ID
        dialog_id = f"{session_id}_{time.time()}"
        
        # FIX: Use lock when modifying shared state
        async with self._lock:
            self.pending_dialogs[dialog_id] = response_future
            self._dialog_timestamps[dialog_id] = time.time()
        
        # Get dialog details
        dialog_type = dialog.type  # 'alert', 'confirm', 'prompt', 'beforeunload'
        dialog_message = dialog.message
        default_value = getattr(dialog, 'default_value', '') if hasattr(dialog, 'default_value') else ''
        
        # Send dialog info to client
        if self.dialog_callback:
            try:
                await asyncio.wait_for(
                    self.dialog_callback({
                        'type': 'browser_dialog',
                        'dialog_type': dialog_type,
                        'message': dialog_message,
                        'default_value': default_value,
                        'dialog_id': dialog_id
                    }),
                    timeout=5.0  # Don't block forever on callback
                )
            except asyncio.TimeoutError:
                logger.warning(f"DialogHandler: Callback timeout for dialog {dialog_id}")
            except Exception as e:
                logger.error(f"DialogHandler: Callback error: {e}")
        
        # Wait for client response (this blocks the browser until client responds)
        # FIX: Use the configured timeout
        try:
            response = await asyncio.wait_for(response_future, timeout=self.dialog_timeout)
            
            # Accept/dismiss the dialog with the response
            if dialog_type == 'prompt':
                await dialog.accept(response if response else "")
            elif dialog_type == 'confirm':
                if response:
                    await dialog.accept()
                else:
                    await dialog.dismiss()
            else:
                await dialog.accept()
            
        except asyncio.TimeoutError:
            # Timeout - dismiss/accept with default
            logger.debug(f"DialogHandler: Dialog {dialog_id} timed out after {self.dialog_timeout}s")
            if dialog_type == 'confirm':
                await dialog.dismiss()
            else:
                await dialog.accept()
        
        finally:
            # FIX: Use lock when cleaning up
            async with self._lock:
                self.pending_dialogs.pop(dialog_id, None)
                self._dialog_timestamps.pop(dialog_id, None)
        
        return response_future.result() if response_future.done() else None
    
    async def respond_to_dialog(self, dialog_id: str, response: str, accepted: bool = True):
        """Called by client to respond to a dialog"""
        async with self._lock:
            if dialog_id in self.pending_dialogs:
                future = self.pending_dialogs[dialog_id]
                if not future.done():
                    future.set_result(response if accepted else None)
    
    async def cleanup_session_dialogs(self, session_id: str):
        """Clean up all pending dialogs for a specific session"""
        async with self._lock:
            dialogs_to_remove = [did for did in self.pending_dialogs.keys() if did.startswith(session_id)]
            for dialog_id in dialogs_to_remove:
                future = self.pending_dialogs.pop(dialog_id, None)
                self._dialog_timestamps.pop(dialog_id, None)
                if future and not future.done():
                    future.set_result(None)  # Resolve without action


class XvfbManager:
    """
    Manages Xvfb (X Virtual Framebuffer) for headless Linux servers.
    Automatically starts Xvfb if needed and assigns display numbers.
    FIXED: All blocking operations replaced with async-compatible versions.
    """
    
    def __init__(self):
        self.process: Optional[subprocess.Popen] = None
        self.display_num: Optional[int] = None
        self.xvfb_available: Optional[bool] = None
        self._check_task = None
        self._lock = asyncio.Lock()
        self._startup_complete = asyncio.Event()
        self._last_start_error: Optional[str] = None
        # NOTE: the Xvfb availability check is LAZY on purpose - it must not
        # create asyncio tasks here, because this constructor can run outside
        # a running event loop (sync app startup) where create_task() crashes.

    def ensure_checked(self) -> bool:
        """Synchronous (cached) Xvfb availability check for non-async contexts."""
        if self.xvfb_available is not None:
            return self.xvfb_available
        try:
            result = subprocess.run(
                ['which', 'Xvfb'],
                capture_output=True, text=True, timeout=3
            )
            self.xvfb_available = result.returncode == 0
        except Exception:
            self.xvfb_available = False
        return self.xvfb_available

    def try_install(self) -> bool:
        """Best-effort auto-install of Xvfb on Linux (one attempt per process).

        Headless VPSs usually lack the Xvfb *binary*; without it every launch
        path silently collapses to plain headless (loud bot signal) — or used
        to crash when an extension forced headed.  PCM/any session on such a
        host must instead create a virtual screen and go headed on it.

        Disabled with XVFB_AUTOINSTALL=0.  Requires root or sudo; otherwise it
        logs the exact manual command and returns False.
        """
        if getattr(self, '_install_tried', False):
            return bool(self.xvfb_available)
        self._install_tried = True
        if not sys.platform.startswith('linux'):
            return False
        if str(os.environ.get('XVFB_AUTOINSTALL', '1')).strip() in ('0', 'false', 'no', 'off'):
            logger.info("[Xvfb] auto-install disabled via XVFB_AUTOINSTALL=0")
            return False
        if self.ensure_checked():
            return True

        try:
            is_root = (os.geteuid() == 0)
        except Exception:
            is_root = False

        def _run(cmd):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                                   env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'})
                return r.returncode == 0, (r.stderr or r.stdout or '').strip()[-400:]
            except Exception as e:
                return False, str(e)

        pm_candidates = []
        for pm, install_cmd in (
            ('apt-get', [['apt-get', 'install', '-y', 'xvfb'],
                         ['apt-get', 'update'],
                         ['apt-get', 'install', '-y', 'xvfb']]),
            ('dnf',     [['dnf', 'install', '-y', 'xorg-x11-server-Xvfb']]),
            ('yum',     [['yum', 'install', '-y', 'xorg-x11-server-Xvfb']]),
            ('apk',     [['apk', 'add', 'xvfb']]),
            ('pacman',  [['pacman', '-S', '--noconfirm', 'xorg-server-xvfb']]),
            ('zypper',  [['zypper', 'install', '-y', 'xorg-x11-server-Xvfb']]),
        ):
            probe = subprocess.run(['which', pm], capture_output=True, timeout=3)
            if probe.returncode == 0:
                pm_candidates = install_cmd
                break
        if not pm_candidates:
            logger.warning("[Xvfb] no supported package manager found; install manually: apt-get install -y xvfb")
            return False
        if not is_root:
            sudo = subprocess.run(['which', 'sudo'], capture_output=True, timeout=3)
            if sudo.returncode != 0:
                logger.warning("[Xvfb] not root and no sudo; install manually: apt-get install -y xvfb")
                return False
            pm_candidates = [['sudo'] + c for c in pm_candidates]

        for cmd in pm_candidates:
            logger.info(f"[Xvfb] installing virtual display server: {' '.join(cmd)}")
            ok, tail = _run(cmd)
            if not ok:
                logger.warning(f"[Xvfb] step failed ({cmd[0]}): {tail}")
                if cmd[0] in ('apt-get', 'sudo'):   # retry only after 'update'
                    continue
            self.xvfb_available = None
            if self.ensure_checked():
                logger.info("[Xvfb] Xvfb installed successfully — headed Chrome on virtual display is now possible")
                return True

        self.xvfb_available = None
        ok = self.ensure_checked()
        logger.warning(f"[Xvfb] install attempted, Xvfb still {'FOUND' if ok else 'MISSING'}")
        return ok

    async def _async_check_xvfb(self):
        """Check if Xvfb is installed on the system - async version"""
        try:
            # FIX: Use asyncio.create_subprocess_exec instead of subprocess.run
            proc = await asyncio.create_subprocess_exec(
                'which', 'Xvfb',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
                self.xvfb_available = proc.returncode == 0
            except asyncio.TimeoutError:
                proc.kill()
                self.xvfb_available = False
        except Exception:
            self.xvfb_available = False
    
    async def _check_xvfb_async(self):
        """Async wrapper to ensure xvfb check is complete (starts the check lazily)"""
        if self.xvfb_available is None:
            if self._check_task is None:
                self._check_task = asyncio.create_task(self._async_check_xvfb())
            try:
                await asyncio.wait_for(asyncio.shield(self._check_task), timeout=10.0)
            except Exception:
                if self.xvfb_available is None:
                    self.xvfb_available = False
        return self.xvfb_available
    
    def is_linux_headless(self) -> bool:
        """Check if running on Linux without a display (X11 or Wayland)"""
        return (sys.platform.startswith('linux')
                and not os.environ.get('DISPLAY')
                and not os.environ.get('WAYLAND_DISPLAY'))
    
    async def start_async(self, display_num: int = 99) -> bool:
        """Start Xvfb on a free display, retrying races and stale slots.

        The old implementation only inspected ``:99`` through ``:108`` and
        discarded Xvfb's stderr. On hosts with a supervisor-owned X server,
        stale lock files, or several worker processes, every candidate could
        be rejected even though another display was available. Keep the SB
        path Xvfb-only, but search a wider range and verify each child before
        publishing ``DISPLAY``.
        """
        async with self._lock:
            if self.process is not None:
                if self.process.returncode is None:
                    self._startup_complete.set()
                    return True
                # The previous child died between launches. Drop its handle
                # before searching for a replacement display.
                self.process = None
                self.display_num = None

            # Wait for xvfb check to complete.
            await self._check_xvfb_async()
            if not self.xvfb_available:
                self._last_start_error = "Xvfb executable is not available"
                return False

            self._last_start_error = None
            # Do not assume the traditional :99-:108 range is free. The
            # manager is process-global, while deployments may also run a
            # display manager, browser workers, or another service using it.
            candidates = range(display_num, max(display_num + 100, 200))
            for d in candidates:
                lock_path = f'/tmp/.X{d}-lock'
                socket_path = f'/tmp/.X11-unix/X{d}'
                if os.path.exists(lock_path) or os.path.exists(socket_path):
                    continue

                proc = None
                try:
                    # Capture only stderr so a failed Xvfb gives the operator
                    # the real reason (permissions, stale display, missing
                    # extension, etc.) without buffering normal stdout.
                    proc = await asyncio.create_subprocess_exec(
                        'Xvfb', f':{d}', '-screen', '0', '1920x1080x24', '-ac',
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.sleep(0.5)
                    if proc.returncode is not None:
                        error = b''
                        if proc.stderr is not None:
                            try:
                                error = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                            except Exception:
                                pass
                        detail = error.decode('utf-8', errors='replace').strip()
                        self._last_start_error = (
                            f"Xvfb :{d} exited with code {proc.returncode}"
                            + (f": {detail[-500:]}" if detail else "")
                        )
                        continue

                    self.process = proc
                    self.display_num = d
                    os.environ['DISPLAY'] = f':{d}'
                    self._startup_complete.set()
                    return True
                except Exception as exc:
                    self._last_start_error = f"Xvfb :{d} launch failed: {exc}"
                    if proc is not None and proc.returncode is None:
                        try:
                            proc.terminate()
                            await proc.wait()
                        except Exception:
                            pass
                    continue

            if self._last_start_error is None:
                self._last_start_error = (
                    f"no free X display found in :{display_num}-:{max(display_num + 99, 199)}"
                )
            return False
    
    def start(self, display_num: int = 99) -> bool:
        """Synchronous wrapper - CRITICAL FIX: Never block in async context"""
        # If we're in an async context, we MUST use start_async()
        # Never fall back to sync operations - this was causing the startup delay!
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in async context - this should NEVER call sync methods
                # Instead, schedule the async start and return immediately
                # The caller should await start_async() directly!
                logger.debug("Xvfb.start() called in async context - use start_async() instead")
                # If already started, return True immediately
                if self.process is not None:
                    return True
                # Try to get running loop and use run_in_executor for true async
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(self._start_sync, display_num)
                    return future.result(timeout=10)
            else:
                return loop.run_until_complete(self.start_async(display_num))
        except Exception as e:
            logger.error(f"Xvfb start error: {e}")
            return self._start_sync(display_num)
    
    def _start_sync(self, display_num: int = 99) -> bool:
        """Synchronous Xvfb start for non-async contexts"""
        if self.process is not None:
            return True
        
        try:
            result = subprocess.run(
                ['which', 'Xvfb'],
                capture_output=True,
                text=True,
                timeout=5
            )
            self.xvfb_available = result.returncode == 0
        except Exception:
            self.xvfb_available = False
        
        if not self.xvfb_available:
            return False
        
        for d in range(display_num, display_num + 10):
            lock_path = f'/tmp/.X{d}-lock'
            socket_path = f'/tmp/.X11-unix/X{d}'
            if not os.path.exists(lock_path) and not os.path.exists(socket_path):
                self.display_num = d
                break
        else:
            return False
        
        try:
            self.process = subprocess.Popen(
                ['Xvfb', f':{self.display_num}', '-screen', '0', '1920x1080x24', '-ac'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            # Give Xvfb time to start (sync context - plain sleep is correct here)
            time.sleep(0.5)
            os.environ['DISPLAY'] = f':{self.display_num}'
            self._startup_complete.set()
            return True
        except Exception:
            self.process = None
            return False
    
    async def stop_async(self):
        """Stop Xvfb process - async version"""
        async with self._lock:
            if self.process:
                self.process.terminate()
                try:
                    # FIX: Use async wait with timeout
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except asyncio.TimeoutExpired:
                    self.process.kill()
                    await self.process.wait()
                except Exception:
                    pass
                self.process = None
            self.display_num = None
            self._startup_complete.clear()
    
    def stop(self):
        """Stop Xvfb process - sync wrapper"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Can't await in sync context easily, just terminate
                if self.process:
                    self.process.terminate()
                    self.process = None
                self.display_num = None
                self._startup_complete.clear()
            else:
                loop.run_until_complete(self.stop_async())
        except Exception:
            if self.process:
                self.process.terminate()
                self.process = None
            self.display_num = None
    
    def get_display(self) -> Optional[str]:
        """Get current display string"""
        if self.display_num is not None:
            return f':{self.display_num}'
        return os.environ.get('DISPLAY')


class StealthBrowserConfig:
    """
    Comprehensive stealth browser configuration.
    Provides all necessary flags and settings for undetectable browser operation.
    """
    
    @staticmethod
    def _is_running_as_root() -> bool:
        """Check if running as root user"""
        import os
        return os.geteuid() == 0 if hasattr(os, 'geteuid') else False
    
    @staticmethod
    def _is_containerized() -> bool:
        """Check if running in a container environment"""
        import os
        return (
            os.path.exists('/.dockerenv') or
            os.path.exists('/run/.containerenv') or
            os.environ.get('CONTAINER_ID') or
            os.environ.get('DOCKER_CONTAINER') or
            os.path.exists('/sys/fs/cgroup') and 
            any('docker' in open('/proc/1/cgroup', 'r').read().lower() for _ in [1])
        )
    
    @classmethod
    def _should_disable_sandbox(cls) -> bool:
        """
        Determine if sandbox should be disabled.
        Only disable sandbox when running as root in containerized environments
        where the container already provides isolation.
        """
        return cls._is_running_as_root() or cls._is_containerized()
    
    @classmethod
    def get_base_flags(cls) -> List[str]:
        """
        Base launch flags for a browser that behaves like a REAL user's browser.

        Deliberately ABSENT from this list (each is a known bot signal or a
        measurable difference from normal Chrome):
          --kiosk / --start-minimized       real users browse in a normal window
          --hide-scrollbars                 real scrollbars keep window geometry honest
          --no-zygote                       real Chrome uses the zygote process
          --disable-ipc-flooding-protection present in virtually every puppeteer/Playwright bot
          --disable-features=IsolateOrigins,site-per-process  real Chrome isolates sites
          --disable-extensions              real users have extensions
          --virtual-time-budget             distorts timers; known CDP-automation signal
          --disable-features=WebAuthentication*  real desktop Chrome has WebAuthn
          forced --lang / --accept-lang     the user's language is not the server's
          --disable-sync / --disable-translate  real signed-in users have both
        """
        base_flags: List[str] = []

        # Sandbox: only disable when the environment requires it (root/container).
        # A normal user's Chrome runs sandboxed.
        if cls._should_disable_sandbox():
            base_flags.extend([
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
            ])

        # Hide the CDP automation marker (navigator.webdriver). This is the one
        # anti-automation flag a "real" browser cannot have while CDP is attached.
        base_flags.extend([
            '--disable-blink-features=AutomationControlled',
            '--exclude-switches=enable-automation',
        ])

        # First-run noise (no behavioural difference, keeps fresh profiles clean)
        base_flags.extend([
            '--no-first-run',
            '--no-default-browser-check',
            '--disable-default-apps',
            '--disable-first-run-ui',
        ])

        # Stability / quiet operation (not detectable from web JS)
        base_flags.extend([
            '--mute-audio',
            '--disable-crash-reporter',
            '--no-crash-upload',
            '--metrics-recording-only',
            '--silent-debugger-extension-api',
            '--disable-client-side-phishing-detection',
            '--allow-insecure-localhost',
        ])

        return base_flags

    # Headless is a LAST resort (see PlatformRuntime). When we must use it,
    # only add the mode flag - no virtual-time tricks, no timer no-throttling
    # hacks (those change measurable page behaviour vs a real browser).
    HEADLESS_FLAGS = [
        '--headless=new',
    ]

    MOBILE_FLAGS = [
        '--touch-events=enabled',
        '--overscroll-history-navigation=disabled',
    ]

    @classmethod
    def get_flags(cls, is_headless: bool = False, is_mobile: bool = False,
                  locale: str = None, gpu: str = 'auto',
                  hide_scrollbars: bool = None) -> List[str]:
        """
        Get the full launch flag list for the requested mode.
        Delegates to DirectChromeLauncher.build_real_flags (single source of
        truth shared by every launch path in this module).
        """
        return DirectChromeLauncher.build_real_flags(
            is_mobile=is_mobile,
            headless=is_headless,
            locale=locale,
            gpu=gpu,
            hide_scrollbars=hide_scrollbars,
        )


# Global Xvfb manager instance
_xvfb_manager = None


def get_xvfb_manager() -> XvfbManager:
    """Get or create global Xvfb manager"""
    global _xvfb_manager
    if _xvfb_manager is None:
        _xvfb_manager = XvfbManager()
    return _xvfb_manager


class UserProfileManager:
    """
    Manages persistent user profiles for session transfer capability.
    Each user has their own profile folder containing:
    - About.txt: User info (IP, user agent, etc.)
    - cookies.json: Per-site cookies (auto-updating)
    - Local Storage: IndexedDB and LocalStorage data
    - Session Storage: Session-specific data
    """
    
    def __init__(self, config):
        self.config = config
        self.base_dir = Path(config.profile_base_path)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._user_metadata: Dict[str, Dict] = {}
    
    def get_user_profile_path(self, user_id: str) -> Path:
        """Get profile directory for user - creates full structure if not exists."""
        profile_path = self.base_dir / user_id
        # Directory creation participates in the same process-wide lock as
        # metadata/cookie IO, so a cleanup cannot race a partially-created
        # profile tree.
        with _profile_io_lock(profile_path):
            profile_path.mkdir(parents=True, exist_ok=True)
            (profile_path / "Default").mkdir(parents=True, exist_ok=True)
            (profile_path / "Local Storage").mkdir(parents=True, exist_ok=True)
            (profile_path / "Session Storage").mkdir(parents=True, exist_ok=True)
            (profile_path / "Extension State").mkdir(parents=True, exist_ok=True)
            (profile_path / "Service Worker").mkdir(parents=True, exist_ok=True)
        return profile_path
    
    def get_or_create_profile(self, user_id: str) -> Path:
        """Get existing profile or create new one"""
        return self.get_user_profile_path(user_id)
    
    def cleanup_profile(self, user_id: str) -> None:
        """Remove profile directory for user"""
        profile_path = self.base_dir / user_id
        with _profile_io_lock(profile_path):
            if profile_path.exists():
                for attempt in range(3):
                    try:
                        shutil.rmtree(profile_path, ignore_errors=True)
                        break
                    except PermissionError:
                        # Use non-blocking approach - just retry immediately
                        time.sleep(0.1)
    
    def profile_exists(self, user_id: str) -> bool:
        """Check if profile exists for user under the profile lock."""
        profile_path = self.base_dir / user_id
        with _profile_io_lock(profile_path):
            return profile_path.exists()
    
    async def save_user_info(self, user_id: str, info: Dict) -> None:
        """Save user information to About.txt and meta.json"""
        profile_path = self.get_user_profile_path(user_id)
        about_file = profile_path / "About.txt"
        meta_file = profile_path / "meta.json"
        
        current_time = time.strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            # Save About.txt
            with _profile_io_lock(profile_path):
                with open(about_file, 'w', encoding='utf-8') as f:
                    f.write("=" * 60 + "\n")
                    f.write("USER PROFILE INFORMATION\n")
                    f.write("=" * 60 + "\n\n")
                    f.write(f"User ID: {user_id}\n")
                    f.write(f"Created: {info.get('created_at', current_time)}\n")
                    f.write(f"Last Updated: {current_time}\n\n")
                
                    f.write("-" * 40 + "\n")
                    f.write("CONNECTION INFO\n")
                    f.write("-" * 40 + "\n")
                    f.write(f"IP Address: {info.get('ip', 'Unknown')}\n")
                    f.write(f"Port: {info.get('port', 'Unknown')}\n")
                    f.write(f"Protocol: {info.get('protocol', 'https')}\n\n")
                
                    f.write("-" * 40 + "\n")
                    f.write("LOCATION INFO\n")
                    f.write("-" * 40 + "\n")
                    f.write(f"Country: {info.get('country', 'Unknown')}\n")
                    f.write(f"State/Region: {info.get('state', 'Unknown')}\n\n")
                
                    f.write("-" * 40 + "\n")
                    f.write("BROWSER INFO\n")
                    f.write("-" * 40 + "\n")
                    f.write(f"User Agent: {info.get('user_agent', 'Unknown')}\n")
                    f.write(f"Browser: {info.get('browser', 'Chrome')}\n")
                    f.write(f"Browser Version: {info.get('browser_version', 'Unknown')}\n")
                    f.write(f"Platform: {info.get('platform', 'Windows')}\n")
                    f.write(f"Viewport: {info.get('viewport', 'Unknown')}\n")
                    f.write(f"Pixel Ratio: {info.get('pixel_ratio', '1.0')}\n\n")
                
                    f.write("-" * 40 + "\n")
                    f.write("SESSION INFO\n")
                    f.write("-" * 40 + "\n")
                    f.write(f"Session Started: {info.get('session_start', 'Unknown')}\n")
                    f.write(f"Total Sessions: {info.get('total_sessions', 1)}\n")
                    f.write(f"Current URL: {info.get('current_url', 'None')}\n\n")
                
                    f.write("-" * 40 + "\n")
                    f.write("PROFILE SETTINGS\n")
                    f.write("-" * 40 + "\n")
                    f.write(f"Profile Enabled: {info.get('profile_enabled', True)}\n")
                    f.write(f"Stealth Mode: {info.get('stealth_mode', True)}\n")
                    f.write(f"Headless Mode: {info.get('headless', False)}\n")
                    f.write(f"GPU Mode: {info.get('gpu_mode', 'CPU')}\n\n")
                
                    f.write("=" * 60 + "\n")
                    f.write("AUTO-GENERATED BY NEO BROWSER STREAM\n")
                    f.write("=" * 60 + "\n")
            
            # Save meta.json (for Admin panel)
            meta_data = {
                "username": user_id,
                "status": info.get('status', 'offline'),
                "last_active": current_time,
                "created_at": info.get('created_at', current_time),
                "current_url": info.get('current_url', ''),
                "total_sessions": info.get('total_sessions', 1),
                "sites_count": 0
            }
            # Merge with the latest metadata under the shared parent lock; a
            # reconnect from another runtime session must not erase status or
            # URL fields written milliseconds earlier.
            with _profile_io_lock(profile_path):
                latest = {}
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            latest = loaded
                    except Exception:
                        pass
                # ``save_user_info`` is connection metadata, not an
                # authoritative liveness update. Preserve fields owned by
                # heartbeat/status writers unless this call explicitly set
                # them.
                if "status" not in info:
                    meta_data.pop("status", None)
                if "current_url" not in info:
                    meta_data.pop("current_url", None)
                if "total_sessions" not in info:
                    meta_data.pop("total_sessions", None)
                if "sites_count" not in info:
                    meta_data.pop("sites_count", None)
                latest.update(meta_data)
                _atomic_json_write(meta_file, latest)
                
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    async def update_status(self, user_id: str, status: str, current_url: str = '') -> None:
        """Update profile status without losing a concurrent session's fields."""
        profile_path = self.get_user_profile_path(user_id)
        meta_file = profile_path / "meta.json"
        try:
            with _profile_io_lock(profile_path):
                meta_data = {}
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as f:
                            loaded = json.load(f)
                        if isinstance(loaded, dict):
                            meta_data = loaded
                    except Exception:
                        pass
                meta_data['status'] = status
                meta_data['last_active'] = time.strftime('%Y-%m-%d %H:%M:%S')
                if current_url:
                    meta_data['current_url'] = current_url
                _atomic_json_write(meta_file, meta_data)
        except Exception as e:
            logger.error(f"[Profile Error] {e}")

    async def add_visited_site(self, user_id: str, domain: str, title: str = '', favicon_url: str = '') -> None:
        """Merge a visited site without losing another session's update."""
        profile_path = self.get_user_profile_path(user_id)
        sites_file = profile_path / "visited_sites.json"
        favicons_dir = profile_path / "favicons"
        favicon_path = favicons_dir / f"{domain.replace('.', '_')}.png"

        try:
            with _profile_io_lock(profile_path):
                favicons_dir.mkdir(exist_ok=True)
            # Network IO is deliberately outside the profile lock.  The final
            # read-modify-write below is repeated after the download so two
            # concurrent tabs merge against the newest list.
            saved_favicon_url = await self._download_favicon(domain, favicon_path)
            with _profile_io_lock(profile_path):
                favicon_local = (
                    str(favicon_path.relative_to(profile_path))
                    if favicon_path.exists() else ""
                )
            site_entry = {
                "domain": domain,
                "favicon_url": saved_favicon_url or favicon_url or f"https://{domain}/favicon.ico",
                "favicon_local": favicon_local,
                "last_visited": time.strftime('%Y-%m-%d %H:%M:%S'),
                "title": title,
            }
            with _profile_io_lock(profile_path):
                sites = []
                if sites_file.exists():
                    try:
                        with open(sites_file, 'r', encoding='utf-8') as f:
                            loaded = json.load(f)
                        if isinstance(loaded, list):
                            sites = [item for item in loaded if isinstance(item, dict)]
                    except Exception:
                        pass
                existing_idx = next(
                    (i for i, site in enumerate(sites)
                     if site.get('domain') == domain),
                    None,
                )
                if existing_idx is None:
                    sites.insert(0, site_entry)
                else:
                    sites[existing_idx] = site_entry
                sites = sites[:100]
                _atomic_json_write(sites_file, sites)

                meta_file = profile_path / "meta.json"
                if meta_file.exists():
                    try:
                        with open(meta_file, 'r', encoding='utf-8') as f:
                            meta_data = json.load(f)
                        if isinstance(meta_data, dict):
                            meta_data['sites_count'] = len(sites)
                            _atomic_json_write(meta_file, meta_data)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"[Profile Error] {e}")

    async def _download_favicon(self, domain: str, favicon_path: Path) -> str:
        """Download favicon for a domain and save to profile folder"""
        try:
            # Try to download favicon
            favicon_url = f"https://{domain}/favicon.ico"
            req = urllib.request.Request(favicon_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = response.read()
                # Save as PNG if valid image
                if data and len(data) > 0:
                    # Avoid readers observing a partially-written favicon.
                    fd, tmp_name = tempfile.mkstemp(
                        prefix=f".{favicon_path.name}.",
                        suffix=".tmp",
                        dir=str(favicon_path.parent),
                    )
                    try:
                        with os.fdopen(fd, 'wb') as f:
                            f.write(data)
                            f.flush()
                            os.fsync(f.fileno())
                        os.replace(tmp_name, favicon_path)
                    finally:
                        try:
                            os.unlink(tmp_name)
                        except FileNotFoundError:
                            pass
                    return f"/api/profiles/favicons/{favicon_path.name}"
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        # Return default URL if download fails
        return f"https://{domain}/favicon.ico"
    
    def get_site_favicon(self, user_id: str, domain: str) -> str:
        """Get favicon path for a site"""
        profile_path = self.get_user_profile_path(user_id)
        favicon_path = profile_path / "favicons" / f"{domain.replace('.', '_')}.png"
        with _profile_io_lock(profile_path):
            if favicon_path.exists():
                return f"/api/profiles/favicons/{favicon_path.name}"
        return f"https://{domain}/favicon.ico"
    
    async def _update_sites_count(self, user_id: str, count: int) -> None:
        """Update sites count in meta.json"""
        profile_path = self.get_user_profile_path(user_id)
        meta_file = profile_path / "meta.json"
        
        try:
            with _profile_io_lock(profile_path):
                if meta_file.exists():
                    with open(meta_file, 'r', encoding='utf-8') as f:
                        meta_data = json.load(f)
                    if isinstance(meta_data, dict):
                        meta_data['sites_count'] = count
                        _atomic_json_write(meta_file, meta_data)
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    def get_visited_sites(self, user_id: str) -> List[Dict]:
        """Get list of visited sites for a profile"""
        profile_path = self.get_user_profile_path(user_id)
        sites_file = profile_path / "visited_sites.json"
        
        try:
            with _profile_io_lock(profile_path):
                if sites_file.exists():
                    with open(sites_file, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    return loaded if isinstance(loaded, list) else []
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return []
    
    def get_profile_meta(self, user_id: str) -> Dict:
        """Get profile metadata"""
        profile_path = self.get_user_profile_path(user_id)
        meta_file = profile_path / "meta.json"
        
        try:
            with _profile_io_lock(profile_path):
                if meta_file.exists():
                    with open(meta_file, 'r', encoding='utf-8') as f:
                        loaded = json.load(f)
                    return loaded if isinstance(loaded, dict) else {}
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return {}
    
    def get_all_profiles_info(self) -> List[Dict]:
        """Get info for all profiles (for Admin panel)"""
        profiles = []
        try:
            for profile_dir in sorted(self.base_dir.iterdir()):
                if profile_dir.is_dir() and not profile_dir.name.startswith('.'):
                    user_id = profile_dir.name
                    meta = self.get_profile_meta(user_id)
                    sites = self.get_visited_sites(user_id)
                    cookie_domains = self.get_cookie_domains(user_id)
                    
                    profiles.append({
                        "user_id": user_id,
                        "status": meta.get('status', 'offline'),
                        "last_active": meta.get('last_active', ''),
                        "created_at": meta.get('created_at', ''),
                        "sites_count": len(sites),
                        "current_url": meta.get('current_url', ''),
                        "sites": sites[:20],  # Limit to 20 for display
                        "cookie_domains": cookie_domains[:12]  # Limit to 12 cookie domains for favicons
                    })
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return profiles
    
    async def update_cookies(self, user_id: str, domain: str, cookies: List[Dict]) -> None:
        """Merge one domain's cookies under the parent-profile file lock."""
        profile_path = self.get_user_profile_path(user_id)
        cookies_file = profile_path / "cookies.json"
        try:
            with _profile_io_lock(profile_path):
                current: List[Dict] = []
                if cookies_file.exists():
                    try:
                        with open(cookies_file, 'r', encoding='utf-8') as f:
                            raw = json.load(f)
                        if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
                            current = [c for c in raw["cookies"] if isinstance(c, dict)]
                        elif isinstance(raw, dict):
                            for value in raw.values():
                                if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                                    current.extend(c for c in value["cookies"] if isinstance(c, dict))
                    except Exception:
                        pass
                incoming = [dict(c) for c in (cookies or []) if isinstance(c, dict)]
                by_key = {
                    (str(c.get("name", "")), str(c.get("domain", "")), str(c.get("path", "/"))): c
                    for c in current
                }
                for cookie in incoming:
                    cookie.setdefault("domain", domain)
                    by_key[(str(cookie.get("name", "")), str(cookie.get("domain", domain)), str(cookie.get("path", "/")))] = cookie
                merged = list(by_key.values())
                _atomic_json_write(cookies_file, {
                    "saved_at": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "cookie_count": len(merged),
                    "cookies": merged,
                })
        except Exception as e:
            logger.error(f"[Profile Error] {e}")

    async def save_cookies(self, user_id: str, cookies: List[Dict]) -> None:
        """Merge a session snapshot into the durable cookie set atomically."""
        profile_path = self.get_user_profile_path(user_id)
        cookies_file = profile_path / "cookies.json"
        try:
            with _profile_io_lock(profile_path):
                existing: List[Dict] = []
                if cookies_file.exists():
                    try:
                        with open(cookies_file, 'r', encoding='utf-8') as f:
                            raw = json.load(f)
                        if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
                            existing = [c for c in raw["cookies"] if isinstance(c, dict)]
                        elif isinstance(raw, dict):
                            for value in raw.values():
                                if isinstance(value, dict) and isinstance(value.get("cookies"), list):
                                    existing.extend(c for c in value["cookies"] if isinstance(c, dict))
                    except Exception:
                        pass
                by_key = {
                    (str(c.get("name", "")), str(c.get("domain", "")), str(c.get("path", "/"))): c
                    for c in existing
                }
                for cookie in (cookies or []):
                    if not isinstance(cookie, dict):
                        continue
                    key = (str(cookie.get("name", "")), str(cookie.get("domain", "")), str(cookie.get("path", "/")))
                    by_key[key] = dict(cookie)
                merged = list(by_key.values())
                _atomic_json_write(cookies_file, {
                    "saved_at": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "cookie_count": len(merged),
                    "cookies": merged,
                })
        except Exception as e:
            logger.error(f"[Profile Error] {e}")

    async def load_cookies(self, user_id: str) -> List[Dict]:
        """Load cookies from cookies.json"""
        profile_path = self.get_user_profile_path(user_id)
        cookies_file = profile_path / "cookies.json"
        
        try:
            with _profile_io_lock(profile_path):
                if cookies_file.exists():
                    with open(cookies_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if isinstance(data, dict) and isinstance(data.get('cookies'), list):
                        return list(data['cookies'])
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return []
    
    def get_cookie_domains(self, user_id: str) -> List[Dict]:
        """Get cookie domains under the shared profile read lock."""
        profile_path = self.get_user_profile_path(user_id)
        cookies_file = profile_path / "cookies.json"
        try:
            with _profile_io_lock(profile_path):
                if not cookies_file.exists():
                    return []
                with open(cookies_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                domains = set()
                if isinstance(data, dict) and isinstance(data.get("cookies"), list):
                    for cookie in data["cookies"]:
                        if isinstance(cookie, dict) and cookie.get("domain"):
                            domains.add(str(cookie["domain"]))
                elif isinstance(data, dict):
                    # Legacy format: {domain: {cookies/data/...}}
                    for domain, domain_data in data.items():
                        if domain not in {"saved_at", "cookie_count"} and isinstance(domain_data, dict):
                            domains.add(str(domain))
                return [
                    {
                        "domain": domain,
                        "favicon_url": f"https://{domain}/favicon.ico",
                        "updated_at": "",
                    }
                    for domain in sorted(domains)
                ]
        except Exception as e:
            logger.error(f"[Profile Error] {e}")
        return []

    async def save_local_storage(self, user_id: str, origin: str, data: Dict) -> None:
        """Atomically save local storage for one origin."""
        profile_path = self.get_user_profile_path(user_id)
        safe_origin = (origin.replace('https://', '').replace('http://', '')
                       .replace('.', '_').replace('/', '_').replace('\\', '_'))
        storage_file = profile_path / "Local Storage" / f"{safe_origin}.json"
        try:
            with _profile_io_lock(profile_path):
                storage_file.parent.mkdir(parents=True, exist_ok=True)
                _atomic_json_write(storage_file, {
                    "origin": origin,
                    "saved_at": time.strftime('%Y-%m-%d %H:%M:%S'),
                    "data": data if isinstance(data, dict) else {},
                })
        except Exception as e:
            logger.error(f"[Profile Error] {e}")

    async def load_local_storage(self, user_id: str) -> Dict:
        """Load a consistent snapshot of all local storage data."""
        profile_path = self.get_user_profile_path(user_id)
        storage_dir = profile_path / "Local Storage"
        result = {}
        try:
            with _profile_io_lock(profile_path):
                if storage_dir.exists():
                    for file in storage_dir.glob("*.json"):
                        try:
                            with open(file, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            if isinstance(data, dict):
                                result[data.get('origin', file.stem)] = data.get('data', {})
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"[Profile Error] {e}")
        return result

    def get_all_profiles(self) -> List[Dict]:
        """Get a consistent list of user profiles with metadata."""
        profiles = []
        try:
            for profile_dir in self.base_dir.iterdir():
                if not profile_dir.is_dir() or profile_dir.name.startswith('.'):
                    continue
                about_file = profile_dir / "About.txt"
                cookies_file = profile_dir / "cookies.json"
                profile_info = {
                    "user_id": profile_dir.name,
                    "path": str(profile_dir),
                    "exists": True,
                }
                with _profile_io_lock(profile_dir):
                    profile_info["about_exists"] = about_file.exists()
                    if cookies_file.exists():
                        try:
                            with open(cookies_file, 'r', encoding='utf-8') as f:
                                data = json.load(f)
                            profile_info["cookie_count"] = (
                                data.get("cookie_count", 0)
                                if isinstance(data, dict) else 0
                            )
                        except Exception:
                            profile_info["cookie_count"] = 0
                profiles.append(profile_info)
        except Exception as e:
            logger.error(f"[Profile Error] {e}")
        return profiles

    def get_client_ip(self) -> str:
        """Get the client IP address"""
        try:
            # Try to get from environment (set by reverse proxy)
            if os.environ.get('HTTP_X_FORWARDED_FOR'):
                return os.environ['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
            if os.environ.get('HTTP_X_REAL_IP'):
                return os.environ['HTTP_X_REAL_IP']
            
            # Get from socket
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            return local_ip
        except Exception:
            return "127.0.0.1"


class FingerprintManager:
    """
    Manages permanent browser fingerprints for each user profile.
    CRITICAL: Each user gets ONE fingerprint that is generated ONCE and reused forever.
    
    Implements comprehensive fingerprint spoofing:
    1. Network fingerprint - matches proxy location
    2. Browser fingerprint - WebGL, Canvas, Audio
    3. Device fingerprint - CPU, Memory, Touch
    4. Navigator object spoofing
    
    Fingerprint stored in: {profile_dir}/fingerprint.json
    
    IMPORTANT: Never randomize per request - generate ONCE, reuse forever
    """
    
    # Timezone to country mapping for network fingerprint consistency
    TIMEZONE_COUNTRY_MAP = {
        'America/New_York': {'country': 'US', 'language': 'en-US', 'offset': -300},
        'America/Los_Angeles': {'country': 'US', 'language': 'en-US', 'offset': -480},
        'America/Chicago': {'country': 'US', 'language': 'en-US', 'offset': -360},
        'America/Phoenix': {'country': 'US', 'language': 'en-US', 'offset': -420},
        'Europe/London': {'country': 'GB', 'language': 'en-GB', 'offset': 0},
        'Europe/Paris': {'country': 'FR', 'language': 'fr-FR', 'offset': 60},
        'Europe/Berlin': {'country': 'DE', 'language': 'de-DE', 'offset': 60},
        'Asia/Tokyo': {'country': 'JP', 'language': 'ja-JP', 'offset': 540},
        'Asia/Shanghai': {'country': 'CN', 'language': 'zh-CN', 'offset': 480},
        'Asia/Singapore': {'country': 'SG', 'language': 'en-SG', 'offset': 480},
        'Australia/Sydney': {'country': 'AU', 'language': 'en-AU', 'offset': 600},
    }
    
    # Real device configurations for mobile - MUST match User Agent
    # Unified device table (mobile_devices.json - single source of truth).
    # New devices can be added in the JSON without touching code. Split by
    # kind so existing consumers keep their exact contracts.
    MOBILE_DEVICES = {k: v for k, v in _MOBILE_DEVICES_TABLE.items() if v.get('is_mobile')}
    
    # Desktop configurations - MUST match User Agent
    # Same unified table, desktop kind (original three entries - the key set
    # is kept stable for existing consumers).
    DESKTOP_CONFIGS = {
        k: v for k, v in _MOBILE_DEVICES_TABLE.items()
        if not v.get('is_mobile') and k in ('windows_11_chrome', 'windows_11_edge', 'mac_chrome')
    }
    
    def __init__(self, config):
        self.config = config
        self.base_dir = Path(config.profile_base_path)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
    
    def get_fingerprint_path(self, user_id: str) -> Path:
        """Get fingerprint file path for user under its profile lock."""
        profile_path = self.base_dir / user_id
        with _profile_io_lock(profile_path):
            profile_path.mkdir(parents=True, exist_ok=True)
        return profile_path / "fingerprint.json"

    @staticmethod
    def _migrate_apple_mobile_fingerprint(fingerprint: Dict,
                                           user_agent: str) -> Dict:
        """Keep persisted profiles consistent with the Android UA policy."""
        migrated = dict(fingerprint or {})
        migrated.update({
            'user_agent': user_agent,
            'device_type': 'android_device',
            'platform': 'Linux; Android 15',
            'oscpu': 'Linux; Android 15',
            'cpu_cores': 8,
            'memory': 8,
            'max_touch_points': 5,
            'is_mobile': True,
            'has_touch': True,
            'webgl_vendor': 'Google Inc.',
            'webgl_renderer': 'Mali G78',
            'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman'],
        })
        return migrated
    
    def get_fingerprint(self, user_id: str, client_info: Dict = None) -> Dict:
        """
        Get fingerprint for user - creates permanent one if not exists.
        CRITICAL: Each user gets ONE fingerprint that is reused forever.
        
        Args:
            user_id: User identifier
            client_info: Optional dict with client's actual info:
                - user_agent: Client's actual user agent
                - viewport: Client's viewport dict
                - pixel_ratio: Client's device pixel ratio
                - is_mobile: Whether client is mobile
                - timezone: Client's timezone (from proxy location)
                - language: Client's language
                - country: Client's country (from proxy)
                
        Returns:
            Fingerprint dict with all spoofing values
        """
        fingerprint_path = self.get_fingerprint_path(user_id)
        profile_path = fingerprint_path.parent

        # Serialize the complete read/migrate/create/write transaction across
        # BrowserManager/FingerprintManager instances. Otherwise two sessions
        # can both generate a different permanent identity and the last write
        # wins.
        with _profile_io_lock(profile_path):
            # Load existing fingerprint if exists - CRITICAL: reuse forever
            if fingerprint_path.exists():
                try:
                    with open(fingerprint_path, 'r', encoding='utf-8') as f:
                        existing = json.load(f)
                        # Keep the permanent profile stable except for the explicit
                        # Apple-mobile -> Android migration requested by the caller.
                        desired_ua = normalize_mobile_user_agent(
                            (client_info or {}).get('user_agent')
                        )
                        current_is_apple_mobile = is_apple_mobile_user_agent(
                            existing.get('user_agent')
                        )
                        requested_is_mobile = bool((client_info or {}).get('is_mobile')) or is_apple_mobile_user_agent(
                            (client_info or {}).get('user_agent')
                        )
                        if (requested_is_mobile and desired_ua and
                                'Android' in desired_ua and current_is_apple_mobile):
                            existing = self._migrate_apple_mobile_fingerprint(existing, desired_ua)
                            self._save_fingerprint(user_id, existing)
                        if client_info and 'proxy_session' in client_info:
                            existing['proxy_session'] = client_info['proxy_session']
                        return existing
                except Exception:
                    pass

            # Create new fingerprint using client's actual info
            if client_info:
                fingerprint = self._create_fingerprint_from_client(user_id, client_info)
            else:
                # Fallback: create generic desktop fingerprint
                fingerprint = self._create_generic_fingerprint(user_id)

            # Save and return
            self._save_fingerprint(user_id, fingerprint)
            return fingerprint

    def _create_fingerprint_from_client(self, user_id: str, client_info: Dict) -> Dict:
        """
        Create fingerprint using client info with NETWORK CONSISTENCY.
        CRITICAL:
        - Viewport/pixel data stay client-specific; Apple mobile UAs use the
          single Android Chrome identity required by the browser launch policy.
        - Network info from PROXY (timezone, language, country)
        - All spoofing values derived from actual client data
        """
        # Use the normalized browser identity for both backends.  The client
        # viewport remains unchanged; only the Apple mobile UA family is remapped.
        raw_user_agent = client_info.get('user_agent', '')
        user_agent = normalize_mobile_user_agent(raw_user_agent) or ''
        viewport = client_info.get('viewport', {})
        pixel_ratio = client_info.get('pixel_ratio', 1.0)
        is_mobile = bool(client_info.get('is_mobile', False)) or is_apple_mobile_user_agent(raw_user_agent)
        
        # NETWORK INFO: From proxy/geoip - CRITICAL for consistency
        # If proxy = US, then timezone MUST be US timezone
        timezone = client_info.get('timezone', 'America/New_York')
        language = client_info.get('language', 'en-US')
        country = client_info.get('country', 'US')
        proxy_session = client_info.get('proxy_session', user_id)
        
        # Derive spoofing values from client's ACTUAL user agent
        spoofing_config = self._derive_spoofing_from_ua(user_agent, is_mobile)
        
        # Generate consistent seed from user_id for any randomization needed
        seed = self._generate_seed(user_id)
        
        # Get timezone offset for spoofing
        tz_info = self.TIMEZONE_COUNTRY_MAP.get(timezone, {'country': 'US', 'language': 'en-US', 'offset': -300})
        
        # Screen dimensions from client's ACTUAL viewport
        screen_width = viewport.get('width', 1920)
        screen_height = viewport.get('height', 1080)
        
        fingerprint = {
            # Core identity - USE CLIENT'S ACTUAL UA
            'user_id': user_id,
            'proxy_session': proxy_session,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'user_agent': user_agent,  # Client's actual UA, not remapped
            
            # Device configuration - USE CLIENT'S ACTUAL VALUES
            'device_type': spoofing_config['device_type'],
            'viewport': viewport,  # Client's actual viewport
            'pixel_ratio': pixel_ratio,  # Client's actual pixel ratio (informational only)
            'device_scale_factor': 1.0,  # Logical-pixel policy: surface == CSS viewport
            'is_mobile': is_mobile,  # Client's actual mobile flag
            'has_touch': is_mobile,  # Touch follows mobile flag from client
            
            # Platform info - DERIVED from actual UA, not predefined
            'platform': spoofing_config['platform'],
            'oscpu': spoofing_config.get('oscpu', 'Windows NT 10.0; Win64; x64'),
            'cpu_cores': spoofing_config['cpu_cores'],
            'memory': spoofing_config['memory'],
            'max_touch_points': 5 if is_mobile else 0,  # Realistic touch points
            
            # NETWORK CONSISTENCY - From proxy location
            # CRITICAL: All these MUST match the proxy country/timezone
            'timezone': timezone,
            'timezone_offset': tz_info.get('offset', -300),
            'language': language,
            'languages': [language, language.split('-')[0], 'en'],
            'country': country,
            
            # Spoofing values - DERIVED from actual client UA
            'webgl_vendor': spoofing_config['webgl_vendor'],
            'webgl_renderer': spoofing_config['webgl_renderer'],
            'fonts': spoofing_config['fonts'],
            
            # Canvas fingerprint seed (for consistent noise - NOT random per request)
            'canvas_seed': seed,
            
            # Audio fingerprint seed
            'audio_seed': seed,
            
            # Screen info - from client's ACTUAL viewport
            'screen_width': screen_width,
            'screen_height': screen_height,
            'screen_color_depth': 24,
            'screen_pixel_depth': 32,
            'avail_width': screen_width,
            'avail_height': screen_height - 40,  # Account for taskbar
            
            # Connection info
            'connection_type': '4g',
            'connection_downlink': 10,
            'connection_rtt': 50,
            
            # Visual viewport defaults - from client
            'visual_viewport_width': screen_width,
            'visual_viewport_height': screen_height,
        }
        
        return fingerprint
    
    def _derive_spoofing_from_ua(self, user_agent: str, is_mobile: bool) -> Dict:
        """
        Derive spoofing configuration from CLIENT'S ACTUAL user agent.
        IMPORTANT: This only provides spoofing values - we USE the client's actual values
        for UA, viewport, pixel_ratio, etc. This just helps us pick appropriate WebGL,
        fonts, and platform values that match what a real browser on that device would report.
        """
        ua_lower = user_agent.lower()
        
        # Parse the ACTUAL OS versions out of the UA so navigator.platform /
        # oscpu / platformVersion never lie about what the UA claims
        # (a UA saying Android 15 with navigator.platform 'Android 13' is a
        # mismatch device-aware detectors check).
        import re as _re
        _ios_m = _re.search(r'iphone os (\d+_\d+)', ua_lower)
        _and_m = _re.search(r'android (\d+)', ua_lower)
        ios_version = _ios_m.group(1) if _ios_m else '18_3'
        android_version = _and_m.group(1) if _and_m else '15'
        
        # iOS devices - use realistic Apple GPU spoofing
        if 'iphone' in ua_lower or 'ipad' in ua_lower or 'ipod' in ua_lower:
            return {
                'device_type': 'ios_device',
                'platform': 'iPhone',
                'oscpu': f'CPU OS {ios_version} like Mac OS X',
                'cpu_cores': 6,
                'memory': 6,
                'webgl_vendor': 'Apple Inc.',
                'webgl_renderer': 'Apple GPU',
                'fonts': ['SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'Arial', 'Times New Roman']
            }
        
        # Android devices - use realistic ARM GPU spoofing
        elif 'android' in ua_lower:
            return {
                'device_type': 'android_device',
                'platform': f'Linux; Android {android_version}',
                'oscpu': f'Linux; Android {android_version}',
                'cpu_cores': 8,
                'memory': 8,
                'webgl_vendor': 'ARM',
                'webgl_renderer': 'Mali G78',
                'fonts': ['Roboto', 'Noto Sans', 'Helvetica Neue', 'Arial', 'Times New Roman']
            }
        
        # Mac desktop
        elif 'mac' in ua_lower or 'macintosh' in ua_lower:
            return {
                'device_type': 'mac_desktop',
                'platform': 'MacIntel',
                'oscpu': 'Intel Mac OS X 10_15_7',
                'cpu_cores': 8,
                'memory': 16,
                'webgl_vendor': 'Apple Inc.',
                'webgl_renderer': 'Apple GPU',
                'fonts': ['SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'Arial', 'Times New Roman']
            }
        
        # Windows Edge
        elif 'edg' in ua_lower or 'edge' in ua_lower:
            return {
                'device_type': 'windows_edge',
                'platform': 'Win32',
                'oscpu': 'Windows NT 10.0; Win64; x64',
                'cpu_cores': 8,
                'memory': 16,
                'webgl_vendor': 'Google Inc. (NVIDIA)',
                'webgl_renderer': 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0',
                'fonts': ['Segoe UI', 'Arial', 'Times New Roman', 'Calibri', 'Consolas']
            }
        
        # Windows Chrome (default desktop)
        else:
            return {
                'device_type': 'windows_chrome',
                'platform': 'Win32',
                'oscpu': 'Windows NT 10.0; Win64; x64',
                'cpu_cores': 8,
                'memory': 16,
                'webgl_vendor': 'Google Inc. (NVIDIA)',
                'webgl_renderer': 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0',
                'fonts': ['Segoe UI', 'Arial', 'Times New Roman', 'Calibri', 'Consolas']
            }
    
    def _create_generic_fingerprint(self, user_id: str) -> Dict:
        """Create generic desktop fingerprint as fallback"""
        return {
            'user_id': user_id,
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
            'device_type': 'windows_11_chrome',
            'viewport': {'width': 1920, 'height': 1080},
            'pixel_ratio': 1.0,
            'device_scale_factor': 1.0,
            'is_mobile': False,
            'has_touch': False,
            'platform': 'Win32',
            'cpu_cores': 8,
            'memory': 16,
            'max_touch_points': 0,
            'timezone': 'America/New_York',
            'language': 'en-US',
            'country': 'US',
            'webgl_vendor': 'Google Inc. (NVIDIA)',
            'webgl_renderer': 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0',
            'fonts': ['Segoe UI', 'Arial', 'Times New Roman', 'Calibri', 'Consolas'],
            'canvas_seed': self._generate_seed(user_id),
            'audio_seed': self._generate_seed(user_id),
            'screen_width': 1920,
            'screen_height': 1080,
            'screen_color_depth': 24,
            'screen_pixel_depth': 32,
            'connection_type': '4g',
            'connection_downlink': 10,
            'connection_rtt': 50,
        }
    
    def _generate_seed(self, user_id: str) -> int:
        """Generate consistent seed from user_id"""
        hash_val = hash(user_id)
        return abs(hash_val) % 2147483647
    
    def _seeded_random(self, seed: int) -> float:
        """Seeded random for consistent values"""
        seed = (seed * 16807) % 2147483647
        return seed / 2147483647
    
    def _save_fingerprint(self, user_id: str, fingerprint: Dict):
        """Atomically save fingerprint under the durable profile lock."""
        fingerprint_path = self.get_fingerprint_path(user_id)
        try:
            with _profile_io_lock(fingerprint_path.parent):
                _atomic_json_write(fingerprint_path, fingerprint)
            logger.debug(f"[Fingerprint] Saved for user {user_id}")
        except Exception as e:
            logger.error(f"[Fingerprint] Error saving: {e}")
    
    def update_fingerprint(self, user_id: str, updates: Dict):
        """Update fingerprint fields"""
        fingerprint = self.get_fingerprint(user_id)
        fingerprint.update(updates)
        self._save_fingerprint(user_id, fingerprint)
    
    def get_all_fingerprints(self) -> List[Dict]:
        """Get all fingerprints"""
        fingerprints = []
        try:
            for profile_dir in self.base_dir.iterdir():
                if not profile_dir.is_dir() or profile_dir.name.startswith('.'):
                    continue
                fp_path = profile_dir / "fingerprint.json"
                with _profile_io_lock(profile_dir):
                    if fp_path.exists():
                        try:
                            with open(fp_path, 'r', encoding='utf-8') as f:
                                fingerprints.append(json.load(f))
                        except Exception:
                            pass
        except Exception:
            pass
        return fingerprints


class BrowserPool:
    """
    Browser instance pool for reusing browser instances with proper timeout and cleanup.
    FIXED: Added timeout mechanism, automatic cleanup, and proper resource management.
    """
    
    def __init__(self, config, gpu_manager, pool_size: int = 10):
        self.config = config
        self.gpu_manager = gpu_manager
        self.pool_size = pool_size
        self.available_browsers: List = []
        self.lock = asyncio.Lock()
        # FIX: Track checked-out browsers with timestamps for timeout
        self.checked_out_browsers: Dict[str, Dict] = {}
        self.browser_timeout = 60  # 60 seconds timeout for checked-out browsers
        self._cleanup_task = None
        self._running = True

    async def start_cleanup_task(self):
        """Start background cleanup task for stale browsers"""
        self._running = True
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                await self._cleanup_stale_browsers()
            except Exception as e:
                logger.error(f"BrowserPool cleanup error: {e}")

    async def stop_cleanup_task(self):
        """Stop the cleanup task"""
        self._running = False

    async def _cleanup_stale_browsers(self):
        """Clean up browsers that have been checked out too long"""
        async with self.lock:
            current_time = time.time()
            stale_sessions = []
            
            for session_id, browser_info in self.checked_out_browsers.items():
                checkout_time = browser_info.get('checkout_time', 0)
                if current_time - checkout_time > self.browser_timeout:
                    stale_sessions.append(session_id)
            
            for session_id in stale_sessions:
                browser_info = self.checked_out_browsers.pop(session_id, None)
                if browser_info:
                    logger.warning(f"BrowserPool: Force returning stale browser for session {session_id}")
                    await self._force_close_browser(browser_info)
                    # Return to available pool if not too many
                    if len(self.available_browsers) < self.pool_size:
                        browser_info['checkout_time'] = time.time()
                        self.available_browsers.append(browser_info)

    async def _force_close_browser(self, browser_info: Dict):
        """Force close a browser and clean up resources"""
        try:
            if browser_info.get('context'):
                await browser_info['context'].close()
            if browser_info.get('browser'):
                await browser_info['browser'].close()
            if browser_info.get('chrome_process'):
                browser_info['chrome_process'].terminate()
                try:
                    browser_info['chrome_process'].wait(timeout=3)
                except subprocess.TimeoutExpired:
                    browser_info['chrome_process'].kill()
        except Exception as e:
            logger.error(f"Error force closing browser: {e}")

    async def get_browser(self, session_id: str, viewport: Dict,
                          pixel_ratio: float) -> Optional[Any]:
        async with self.lock:
            if self.available_browsers:
                browser_info = self.available_browsers.pop()
                # Track checkout time
                browser_info['checkout_time'] = time.time()
                browser_info['session_id'] = session_id
                self.checked_out_browsers[session_id] = browser_info
                return browser_info
        
        # FIX: If pool is empty, don't just return None - try to create a new one
        logger.debug(f"BrowserPool: No available browsers, pool at capacity ({self.pool_size})")
        return None

    async def return_browser(self, browser_info: Dict, force_close: bool = False) -> None:
        """Return browser to pool or close it if pool is full"""
        session_id = browser_info.get('session_id', 'unknown')
        
        # Remove from checked-out tracking
        async with self.lock:
            self.checked_out_browsers.pop(session_id, None)
        
        if force_close or len(self.available_browsers) >= self.pool_size:
            # Pool full or forced close - close the browser
            logger.debug(f"BrowserPool: Closing browser (pool full={not force_close})")
            await self._force_close_browser(browser_info)
            return
        
        # Return to available pool
        browser_info['checkout_time'] = time.time()
        async with self.lock:
            self.available_browsers.append(browser_info)

    async def release_session(self, session_id: str) -> None:
        """Release a specific session's browser (for cleanup)"""
        async with self.lock:
            browser_info = self.checked_out_browsers.pop(session_id, None)
        if browser_info:
            await self._force_close_browser(browser_info)

    async def cleanup(self) -> None:
        """Clean up all browsers in pool"""
        self._running = False
        async with self.lock:
            # Close all available browsers
            for browser_info in self.available_browsers:
                await self._force_close_browser(browser_info)
            self.available_browsers.clear()
            
            # Close all checked-out browsers
            for browser_info in self.checked_out_browsers.values():
                await self._force_close_browser(browser_info)
            self.checked_out_browsers.clear()


class ChromeCDPConnection:
    """
    Direct Chrome browser connection using Chrome DevTools Protocol (CDP)
    Replaces Playwright for direct Chrome control
    """
    
    def __init__(self, chrome_process: subprocess.Popen, debug_port: int, profile_dir: str):
        self.chrome_process = chrome_process
        self.debug_port = debug_port
        self.profile_dir = profile_dir
        self.ws_url = f"ws://127.0.0.1:{debug_port}/devtools/browser"
        self._cdp_ws = None
        self._connected = False
        self.targets = {}
    
    async def connect(self) -> bool:
        """Connect to Chrome via WebSocket"""
        try:
            # For direct HTTP CDP connection (simpler than WebSocket)
            import urllib.request
            import json
            
            # Get the WebSocket URL from Chrome's JSON endpoint
            json_url = f"http://127.0.0.1:{self.debug_port}/json"
            
            try:
                with urllib.request.urlopen(json_url, timeout=5) as response:
                    data = json.loads(response.read().decode())
                    if data:
                        # Find the browser target
                        for target in data:
                            if target.get('type') == 'browser':
                                self.ws_url = target.get('webSocketDebuggerUrl', self.ws_url)
                                break
            except Exception:
                pass
            
            self._connected = True
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Chrome CDP: {e}")
            return False
    
    async def send_command(self, method: str, params: dict = None,
                           timeout: float = 5.0) -> Optional[dict]:
        """
        Send a CDP command over the browser-level WebSocket (best effort).

        Returns the CDP response dict, or None when the websocket library is
        unavailable or the command fails - callers must treat this as
        optional (the app's own CDP layer may re-assert its own values).
        """
        try:
            import websockets  # ships with playwright
            async with websockets.connect(self.ws_url, open_timeout=timeout) as ws:
                await ws.send(json.dumps({'id': 1, 'method': method, 'params': params or {}}))
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"[CDP] Optional command {method} failed: {e}")
            return None

    async def send_page_command(self, method: str, params: dict = None,
                                timeout: float = 5.0) -> Optional[dict]:
        """Send a CDP command to EVERY open page target.

        Emulation.* and Page.addScriptToEvaluateOnNewDocument are
        page-target-level domains: on the browser-level socket they are
        rejected (silently swallowed by send_command's "best effort"
        fallback), which is why the direct-CDP mobile launch once failed to
        apply the viewport metrics — pages rendered at the wrong width and
        every site showed a white strip on the right.

        Returns the last response dict (or None if no page/no ws support).
        """
        last_res = None
        try:
            import websockets  # ships with playwright
        except Exception:
            return None
        try:
            # Chrome may need a beat after launch before the initial page
            # target appears in /json — retry discovery briefly so the
            # override lands on the actual first tab, not into the void.
            page_targets: List[str] = []
            for _attempt in range(6):
                targets = []
                for ep in ("/json/list", "/json"):
                    try:
                        json_url = f"http://127.0.0.1:{self.debug_port}{ep}"
                        with urllib.request.urlopen(json_url, timeout=timeout) as response:
                            targets = json.loads(response.read().decode())
                        if targets:
                            break
                    except Exception:
                        continue
                page_targets = [
                    t.get('webSocketDebuggerUrl')
                    for t in (targets or [])
                    if isinstance(t, dict) and t.get('type') == 'page' and t.get('webSocketDebuggerUrl')
                ]
                if page_targets:
                    break
                await asyncio.sleep(0.4)
            for page_ws in page_targets:
                try:
                    async with websockets.connect(page_ws, open_timeout=timeout) as ws:
                        await ws.send(json.dumps({'id': 1, 'method': method,
                                                  'params': params or {}}))
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        last_res = json.loads(raw)
                except Exception as e:
                    logger.debug(f"[CDP] page command {method} failed on a target: {e}")
            return last_res
        except Exception as e:
            logger.debug(f"[CDP] page command {method} failed: {e}")
            return None

    async def close(self):
        """Close the CDP connection"""
        try:
            if self.chrome_process:
                self.chrome_process.terminate()
                try:
                    self.chrome_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.chrome_process.kill()
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        self._connected = False
    
    def is_connected(self) -> bool:
        """Check if connection is active"""
        return self._connected and self.chrome_process.poll() is None


class DirectChromeLauncher:
    """
    Launches Chrome directly as a subprocess without Playwright
    Uses Chrome's remote debugging feature for control
    Enhanced with comprehensive stealth mode for undetectable browser operation
    Supports both desktop and mobile device emulation
    """
    
    # Unified device table (mobile_devices.json) - all kinds, including the
    # 'desktop' entry that get_mobile_device_config()/launch_chrome() expect.
    MOBILE_DEVICES = _MOBILE_DEVICES_TABLE
    
    # Launch flags are now built by build_real_flags() - see there for the
    # full rationale. These attributes are kept (slim, realistic) so any
    # external code that reads them directly does not resurrect the old
    # bot-signaling flags (kiosk, no-zygote, ipc-flooding, virtual-time, ...).
    STEALTH_FLAGS = list(StealthBrowserConfig.get_base_flags())
    HEADLESS_FLAGS = list(StealthBrowserConfig.HEADLESS_FLAGS)
    MOBILE_FLAGS = list(StealthBrowserConfig.MOBILE_FLAGS)
    
    @classmethod
    def get_mobile_device_config(cls, device_name: str = None) -> Dict:
        """Get mobile device configuration by name"""
        if device_name and device_name in cls.MOBILE_DEVICES:
            return cls.MOBILE_DEVICES[device_name]
        return cls.MOBILE_DEVICES['desktop']
    
    @classmethod
    def build_real_flags(cls, is_mobile: bool = False, headless: bool = False,
                         locale: str = None, gpu: str = 'auto',
                         hide_scrollbars: bool = None,
                         width: int = None, height: int = None,
                         visible_window: bool = True) -> List[str]:
        """
        SINGLE SOURCE OF TRUTH for launch flags - used by every launch path
        (direct CDP, Playwright create_browser, create_browser_simple).

        Goal: a browser that behaves like a REAL user's browser, on any
        platform (Windows desktop/RDP, Linux VPS via Xvfb, macOS, Termux).

        Deliberately ABSENT (each one is a known bot signal or a measurable
        difference from normal Chrome):
          --kiosk / --start-minimized        real users browse in a normal window
          --no-zygote                        real Chrome uses the zygote process
          --disable-ipc-flooding-protection  present in virtually every puppeteer/Playwright bot
          --disable-features=IsolateOrigins,site-per-process  real Chrome isolates sites
          --disable-extensions               real users have extensions
          --virtual-time-budget              distorts timers; known CDP-automation signal
          --disable-features=WebAuthentication*  real desktop Chrome has WebAuthn
          forced --lang=en-US                the user's language is not the server's
          --disable-background-*             a real active tab is not throttled anyway

        Args:
            locale: the real user's locale (e.g. 'de-DE'). When None, NO
                    language flag is set at all - Chrome then reports the
                    system default, which stays consistent with everything
                    else (never force en-US on a German client's iPhone).
            gpu: 'auto' = let Chrome use the real GPU (visible desktop/RDP),
                 'swiftshader' = force software GL (VPS/Termux without GPU).
            hide_scrollbars: default from env BM_HIDE_SCROLLBARS (off).
        """
        flags: List[str] = list(StealthBrowserConfig.get_base_flags())

        # GPU
        if gpu == 'swiftshader':
            flags += ['--use-angle=swiftshader', '--enable-unsafe-swiftshader',
                      '--ignore-gpu-blocklist']
        # 'auto': no GL flags - Chrome picks the real GPU (or its own fallback),
        # exactly like a normal user's machine.

        # Locale: only ever set from the real client, never forced server-side
        if locale:
            flags.append(f'--lang={locale}')

        # Mobile emulation (touch behaviour of a real phone)
        if is_mobile and ENABLE_MOBILE_STEALTH:
            flags += list(StealthBrowserConfig.MOBILE_FLAGS)

        if headless:
            flags += list(StealthBrowserConfig.HEADLESS_FLAGS)
        elif visible_window and width and height:
            flags += [f'--window-size={int(width)},{int(height)}', '--window-position=0,0']

        if hide_scrollbars is None:
            hide_scrollbars = os.environ.get('BM_HIDE_SCROLLBARS', '0') in ('1', 'true', 'yes')
        if hide_scrollbars:
            flags.append('--hide-scrollbars')

        # De-duplicate, keep order
        return list(dict.fromkeys(flags))

    @classmethod
    def get_stealth_flags(cls, is_headless: bool = False, is_mobile: bool = False,
                          locale: str = None, gpu: str = 'auto',
                          hide_scrollbars: bool = None) -> List[str]:
        """Backward-compatible name. Now returns the realistic flag set."""
        return cls.build_real_flags(
            is_mobile=is_mobile,
            headless=is_headless,
            locale=locale,
            gpu=gpu,
            hide_scrollbars=hide_scrollbars,
            visible_window=not is_headless,
        )
    
    # Candidate executable names checked via PATH on every platform
    _CHROME_PATH_NAMES = [
        'google-chrome', 'google-chrome-stable', 'chrome', 'chromium',
        'chromium-browser', 'chrome-beta', 'chrome-dev',
    ]
    _EDGE_PATH_NAMES = ['msedge', 'microsoft-edge', 'microsoft-edge-stable']

    @staticmethod
    def find_chrome_executable() -> Optional[str]:
        """
        Find a real Chromium-based browser executable on the system.
        Preference: Google Chrome > Microsoft Edge > Chromium.
        Covers Windows (RDP/desktop), Linux (desktop & VPS), macOS and
        Android (Termux).
        """
        import platform as platform_local
        system = platform_local.system()

        def _first_existing(paths: List[str]) -> Optional[str]:
            for path in paths:
                if path and os.path.exists(path):
                    return path
            return None

        def _which(names: List[str]) -> Optional[str]:
            for name in names:
                found = shutil.which(name)
                if found:
                    return found
            return None

        if system == "Windows":
            pf_x86 = os.environ.get("PROGRAMFILES(X86)")
            pf = os.environ.get("PROGRAMFILES")
            local = os.environ.get("LOCALAPPDATA")
            paths = [
                (pf_x86 + "\\Google\\Chrome\\Application\\chrome.exe") if pf_x86 else None,
                (pf + "\\Google\\Chrome\\Application\\chrome.exe") if pf else None,
                "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                (local + "\\Google\\Chrome\\Application\\chrome.exe") if local else None,
                # Edge (real Chromium) as fallback
                (pf_x86 + "\\Microsoft\\Edge\\Application\\msedge.exe") if pf_x86 else None,
                (pf + "\\Microsoft\\Edge\\Application\\msedge.exe") if pf else None,
            ]
            return (
                _first_existing(paths)
                or _which(DirectChromeLauncher._EDGE_PATH_NAMES)
                or _which(DirectChromeLauncher._CHROME_PATH_NAMES)
            )

        if system == "Linux":
            if PlatformRuntime.is_termux():
                # Termux package installs land in the Termux prefix
                return _which(DirectChromeLauncher._CHROME_PATH_NAMES)
            linux_paths = [
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chrome",
                "/usr/local/bin/google-chrome",
                "/opt/google/chrome/google-chrome",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]
            return (_first_existing(linux_paths)
                    or _which(DirectChromeLauncher._CHROME_PATH_NAMES)
                    or _which(DirectChromeLauncher._EDGE_PATH_NAMES))

        if system == "Darwin":  # macOS
            macos_paths = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",  # Homebrew
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            ]
            return _first_existing(macos_paths) or _which(DirectChromeLauncher._CHROME_PATH_NAMES)

        return _which(DirectChromeLauncher._CHROME_PATH_NAMES)
    
    @staticmethod
    async def launch_chrome(
        profile_dir: str,
        viewport_width: int,
        viewport_height: int,
        device_scale_factor: float,
        user_agent: str,
        is_mobile: bool,
        is_headless: bool = False,
        headless: bool = False,
        window_size: str = None,
        additional_args: List[str] = None,
        mobile_device_name: str = None,
        proxy_config: dict = None,
        locale: str = None
    ) -> Optional[tuple]:
        """
        Launch a REAL Chrome with realistic launch flags.

        Launch mode resolution (see PlatformRuntime):
            real display (Windows/RDP, Linux X11, macOS) > Xvfb (Linux VPS)
            > headless (last resort only, e.g. Android/Termux).
        The is_headless/headless parameters are caller hints; when both are
        False the real mode is resolved live, so a Linux VPS runs HEADED on
        Xvfb instead of the bot-signaling headless mode.

        Args:
            locale: the real user's locale (e.g. 'de-DE'). None = do NOT set
                    any language flag (Chrome reports the system default).

        Returns:
            Tuple of (chrome_process, debug_port, profile_dir) or None if failed
        """
        if is_apple_mobile_user_agent(user_agent):
            user_agent = normalize_mobile_user_agent(user_agent)
            is_mobile = True
        import platform as platform_local
        
        chrome_executable = DirectChromeLauncher.find_chrome_executable()
        if not chrome_executable:
            logger.error("Chrome executable not found")
            return None
        
        # Ask Chrome for an ephemeral debug port.  A bind-and-close probe is
        # racy under concurrent launches; Chrome's DevToolsActivePort file is
        # the authoritative collision-free allocation.
        debug_port = 0
        
        # Launch mode: prefer real display / Xvfb virtual display; headless
        # is the last resort because it is the strongest bot signal.
        caller_wants_headless = bool(is_headless or headless)
        resolution = PlatformRuntime.resolve(force_headless=caller_wants_headless)
        launch_mode = resolution['mode']
        xvfb = get_xvfb_manager()
        if launch_mode == PlatformRuntime.MODE_XVFB:
            if xvfb.is_linux_headless() and not xvfb.start():
                logger.error("[DirectLaunch] Xvfb failed to start - falling back to headless")
                launch_mode = PlatformRuntime.MODE_HEADLESS
            elif not xvfb.get_display():
                logger.error("[DirectLaunch] No Xvfb display available - falling back to headless")
                launch_mode = PlatformRuntime.MODE_HEADLESS
        is_headless_effective = (launch_mode == PlatformRuntime.MODE_HEADLESS)
        logger.debug(f"[DirectLaunch] Launch mode: {launch_mode} ({resolution['reason']})")
        
        # GPU: real GPU when a real display exists, SwiftShader on VPS/headless
        gpu_mode = 'auto' if launch_mode == PlatformRuntime.MODE_VISIBLE else 'swiftshader'
        
        # Realistic launch flags (single source of truth for all launch paths)
        real_flags = DirectChromeLauncher.build_real_flags(
            is_mobile=is_mobile,
            headless=is_headless_effective,
            locale=locale,
            gpu=gpu_mode,
            visible_window=False,  # window flags added below with exact viewport
        )
        
        # Get mobile device configuration if specified
        mobile_config = None
        if mobile_device_name and ENABLE_MOBILE_STEALTH:
            mobile_config = DirectChromeLauncher.get_mobile_device_config(mobile_device_name)
        elif is_mobile and ENABLE_MOBILE_STEALTH:
            # Default to a random mobile device if in mobile mode
            mobile_devices = [k for k in DirectChromeLauncher.MOBILE_DEVICES.keys() if k != 'desktop']
            import random
            mobile_device_name = random.choice(mobile_devices)
            mobile_config = DirectChromeLauncher.MOBILE_DEVICES[mobile_device_name]
        if mobile_config:
            # Never let a named/random iPhone preset reintroduce an iOS UA.
            mobile_config = dict(mobile_config)
            mobile_config['user_agent'] = normalize_mobile_user_agent(
                mobile_config.get('user_agent')
            )
        
        # Build Chrome arguments - realistic set from build_real_flags()
        args = [
            chrome_executable,
            f"--remote-debugging-port={debug_port}",
            # Never expose the CDP port to the network (VPS hardening).
            # Ignored harmlessly by Chrome versions that don't support it.
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile_dir}",
        ]
        # LOGICAL-PIXEL POLICY: no surface scaling flags. The browser
        # surface maps 1:1 to the CSS viewport so the screencast frame
        # contains exactly the page content (no browser-area padding).

        if not is_headless_effective:
            # A normal browser window (a real user's browser window).
            # No kiosk, no --start-minimized: window geometry
            # (outerWidth vs innerHeight, screenX/Y) must look like a
            # normal, visible browser.
            args.append(f"--window-size={viewport_width},{viewport_height}")
            args.append("--window-position=0,0")
            if window_size:
                args.append(f"--window-size={window_size}")
        args.extend(real_flags)
        
        # Add user agent (use mobile config's UA if available)
        if mobile_config and mobile_config.get('user_agent'):
            args.append(f"--user-agent={mobile_config['user_agent']}")
        elif user_agent:
            args.append(f"--user-agent={user_agent}")
        
        # Language: only set when the caller knows the real user's locale
        # (locale param -> --lang). Never forced en-US server-side.
        
        # De-duplicate (window-size may appear twice)
        args = list(dict.fromkeys(args))
        
        # Add any additional arguments
        if additional_args:
            args.extend(additional_args)
        
        # ============================================================
        # SingleFile extension mode (Direct Chrome launcher)
        # ============================================================
        # Direct-launch path didn't previously load the extension, so the
        # extension UI / capture was silently disabled when use_direct_chrome
        # was true.  Apply the same resolution rules as the Playwright path
        # so both launch modes behave identically.
        from singlefile_ext import apply_singlefile_ext_args
        args = apply_singlefile_ext_args(
            args, log=lambda m: logger.debug(f"[SINGLEFILE-EXT/Direct] {m}"))

        # NOTE: Do NOT add --proxy-server= argument here!
        # Proxy authentication must be passed to Playwright as a dict with server/username/password
        # The Playwright proxy dict is set in the caller methods (create_browser, create_browser_simple)
        
        # Set environment variables
        env = os.environ.copy()
        env["CHROME_DEVEL_SANDBOX"] = ""
        env["SAFEBROWSING_DISABLE_DOWNLOAD_PROTECTION"] = "1"
        
        try:
            # Start Chrome process
            if platform_local.system() == "Linux":
                # On a headless server the Xvfb display acts as a real
                # monitor: Chrome runs HEADED against the virtual display
                # (launch_mode resolved above). Just make sure the child
                # environment has DISPLAY set.
                display = xvfb.get_display()
                if display:
                    env["DISPLAY"] = display
                elif launch_mode == PlatformRuntime.MODE_XVFB:
                    logger.warning("[DirectLaunch] Xvfb display unavailable - Chrome may fail to start")
            
            # Detach process so it continues running
            if platform_local.system() == "Windows":
                CREATE_NO_WINDOW = 0x08000000
                process = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True
                )
            else:
                # Linux/macOS - use start_new_session to detach from parent
                process = subprocess.Popen(
                    args,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            
            # Wait for Chrome to publish the actual ephemeral debug port.
            # This avoids collisions when several sessions launch together.
            active_port_file = Path(profile_dir) / "DevToolsActivePort"
            port_deadline = time.monotonic() + 10.0
            while time.monotonic() < port_deadline:
                if process.poll() is not None:
                    logger.error("Chrome process exited before publishing DevToolsActivePort")
                    return None
                try:
                    lines = active_port_file.read_text(encoding="utf-8").splitlines()
                    if lines and lines[0].strip().isdigit():
                        debug_port = int(lines[0].strip())
                        if 1 <= debug_port <= 65535:
                            break
                except (FileNotFoundError, OSError, ValueError):
                    pass
                await asyncio.sleep(0.1)
            if not debug_port:
                logger.error("Chrome did not publish a valid DevToolsActivePort")
                try:
                    process.terminate()
                except Exception:
                    pass
                return None

            logger.debug(f"Chrome launched successfully with debug port {debug_port}, mode: {launch_mode}, mobile: {is_mobile}")
            return process, debug_port, profile_dir
            
        except Exception as e:
            logger.error(f"Failed to launch Chrome: {e}")
            return None
    
    @staticmethod
    def _find_available_port(start: int, end: int) -> int:
        """Find an available port in the given range"""
        for port in range(start, end):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    return port
            except OSError:
                continue
        return start  # Default fallback


class BrowserManager:
    """
    Manages browser creation and lifecycle
    Uses real Chrome with persistent profiles for session transfer
    Enhanced with full user profile support
    Cross-platform: Windows (visible), Linux (headless with Xvfb)
    Supports both Playwright and direct Chrome launch modes
    """

    def __init__(self, config, gpu_manager):
        self.config = config
        self.gpu_manager = gpu_manager
        # NOTE: BrowserPool removed - not used, browsers are managed per-session via active_browsers
        self.active_browsers: Dict[str, Dict] = {}
        # Browser ownership is per runtime session.  The lock protects map
        # snapshots/removals used by cleanup and admin paths, while each
        # browser still launches independently.
        self._active_browsers_lock = threading.RLock()
        self._runtime_profile_paths: Dict[str, str] = {}
        self._playwright = None
        self._playwright_lock = asyncio.Lock()
        self.profile_manager = UserProfileManager(config)
        self.fingerprint_manager = FingerprintManager(config)  # NEW: Permanent fingerprint per user
        self._user_ip = None
        
        # Dialog handler for browser dialogs (alert, confirm, prompt, etc.)
        self.dialog_handler = BrowserDialogHandler()
        
        # NEW: Use direct Chrome instead of Playwright
        # Set to True to use Chrome directly via subprocess + CDP
        # Set to False to use Playwright (default behavior)
        self.use_direct_chrome = getattr(config, 'use_direct_chrome', False)  # Default to Playwright
        
        # Initialize Xvfb for Linux headless servers. SeleniumBase owns a
        # private Xvfb process per browser, so do not eagerly create the shared
        # BrowserManager display when SB is selected; Playwright/direct-Chrome
        # paths still start this manager lazily when they actually need it.
        self._xvfb = get_xvfb_manager()
        _backend_hint = os.environ.get('BROWSER_BACKEND', '').strip().lower()
        if not _backend_hint:
            _backend_hint = os.environ.get('PCM_BROWSER_BACKEND', '').strip().lower()
        if not _backend_hint:
            _backend_hint = str(getattr(config, 'browser_backend', '') or '').strip().lower()
        _sb_owns_display = _backend_hint in ('sb', 'seleniumbase', 'uc')
        # Warm package installation off-thread, but never start the shared
        # display on the SB path. This lets SB create the Xvfb it owns at the
        # exact browser launch boundary.
        if (not _sb_owns_display and sys.platform.startswith('linux')
                and not os.environ.get('DISPLAY')
                and not os.environ.get('WAYLAND_DISPLAY')):
            try:
                if not self._xvfb.ensure_checked():
                    import threading as _threading
                    _threading.Thread(target=self._xvfb.try_install, daemon=True,
                                      name='xvfb-warm').start()
            except Exception:
                pass
        initial_mode = PlatformRuntime.resolve()['mode']
        if initial_mode == PlatformRuntime.MODE_XVFB and not _sb_owns_display:
            self._xvfb.start()
        resolved = PlatformRuntime.resolve()
        self._launch_mode = resolved['mode']
        self._is_headless = (self._launch_mode == PlatformRuntime.MODE_HEADLESS)
        logger.debug(
            f"[PlatformRuntime] Launch mode: {self._launch_mode} "
            f"({resolved['reason']})"
        )
        
        # CRITICAL: Check Playwright installation on Windows
        import platform
        if platform.system() == 'Windows':
            logger.debug("=" * 60)
            logger.debug("WINDOWS DETECTED - Checking Playwright installation...")
            self._check_playwright_installation()
            logger.debug("=" * 60)
        
        logger.debug(f"BrowserManager initialized - Direct Chrome: {self.use_direct_chrome}")
    
    async def _check_playwright_installation_async(self):
        """Check if Playwright and browsers are properly installed on Windows (async version)"""
        try:
            from playwright.async_api import async_playwright
            
            logger.debug(f"[CHECK] Playwright async module imported successfully")
            
            # Try to start Playwright and check for Chromium
            async with async_playwright() as p:
                logger.debug(f"[CHECK] Async Playwright started successfully")
                
                # Check if chromium is available
                try:
                    # Try to launch a temporary browser to verify installation
                    browser = await p.chromium.launch(headless=True)
                    browser_type = browser.browser_type
                    await browser.close()
                    logger.debug(f"[CHECK] Chromium launched successfully - Browser type: {browser_type.name}")
                except Exception as e:
                    logger.error(f"[CHECK] Chromium launch failed: {e}")
                    logger.error("FIX: Run 'playwright install chromium' to install browser binaries")
                    logger.error("Or run: python -m playwright install chromium")
        except ImportError as e:
            logger.error(f"[CHECK] Playwright not installed: {e}")
            logger.error("FIX: Run 'pip install playwright' then 'playwright install chromium'")
        except Exception as e:
            logger.error(f"[CHECK] Playwright check failed: {e}")
            import traceback
            logger.error(f"[CHECK] Traceback: {traceback.format_exc()}")
    
    def _check_playwright_installation(self):
        """Check if Playwright and browsers are properly installed on Windows - creates async task"""
        import asyncio
        # Schedule the async check
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._check_playwright_installation_async())
            else:
                loop.run_until_complete(self._check_playwright_installation_async())
        except Exception as e:
            logger.error(f"[CHECK] Could not start async check: {e}")
    
    def _get_client_ip(self) -> str:
        """Get the client IP address"""
        if self._user_ip:
            return self._user_ip
        
        try:
            if os.environ.get('HTTP_X_FORWARDED_FOR'):
                self._user_ip = os.environ['HTTP_X_FORWARDED_FOR'].split(',')[0].strip()
            elif os.environ.get('HTTP_X_REAL_IP'):
                self._user_ip = os.environ['HTTP_X_REAL_IP']
            else:
                hostname = socket.gethostname()
                self._user_ip = socket.gethostbyname(hostname)
        except Exception:
            self._user_ip = "127.0.0.1"
        
        return self._user_ip
    
    def profile_exists(self, user_id: str) -> bool:
        """Check if the durable parent profile exists."""
        return self.profile_manager.profile_exists(user_id)

    def get_session_profile_path(self, user_id: str, session_id: str) -> Path:
        """Return an isolated live Chrome user-data directory.

        The stable parent profile is used for metadata, fingerprints and the
        serialized cookie store only.  Chrome never runs against that directory
        directly: every runtime session gets a private user-data-dir derived
        from both parent and runtime ids.
        """
        parent_id = str(user_id or 'anonymous')
        runtime_id = str(session_id or 'runtime')
        parent_key = hashlib.sha256(parent_id.encode('utf-8', 'replace')).hexdigest()[:24]
        runtime_key = hashlib.sha256(runtime_id.encode('utf-8', 'replace')).hexdigest()[:32]
        root = Path(self.config.profile_base_path) / '.runtime_sessions' / parent_key
        path = root / runtime_key
        path.mkdir(parents=True, exist_ok=True)
        with self._active_browsers_lock:
            self._runtime_profile_paths[runtime_id] = str(path)
        try:
            if hasattr(self.gpu_manager, 'register_runtime_profile'):
                self.gpu_manager.register_runtime_profile(runtime_id, str(path))
        except Exception:
            logger.debug("[Profile] runtime ownership registration failed", exc_info=True)
        return path

    def register_active_browser(self, session_id: str, browser, context,
                                user_id: str, profile_dir: str, gpu_id: int = None):
        """Register any backend's live handle under its runtime session id."""
        info = {
            'browser': browser,
            'context': context,
            'user_id': user_id,
            'session_id': session_id,
            'profile_dir': profile_dir,
            'gpu_id': gpu_id,
            'device_scale_factor': 1.0,
        }
        with self._active_browsers_lock:
            self.active_browsers[session_id] = info
        try:
            if hasattr(self.gpu_manager, 'register_runtime_profile'):
                self.gpu_manager.register_runtime_profile(session_id, profile_dir)
        except Exception:
            logger.debug("[Profile] runtime ownership registration failed", exc_info=True)
        return info

    def get_active_browser(self, session_id: str) -> Optional[Dict]:
        """Return a session browser record under the ownership lock."""
        with self._active_browsers_lock:
            return self.active_browsers.get(session_id)

    def snapshot_active_browsers(self) -> Dict[str, Dict]:
        """Return a shallow map snapshot for diagnostic/admin scans."""
        with self._active_browsers_lock:
            return dict(self.active_browsers)

    async def remove_active_browser(self, session_id: str, *, remove_profile: bool = True):
        """Forget one browser handle after its owner has closed it."""
        with self._active_browsers_lock:
            info = self.active_browsers.pop(session_id, None)
            profile_dir = (info or {}).get('profile_dir') or self._runtime_profile_paths.pop(session_id, None)
            if session_id in self._runtime_profile_paths:
                profile_dir = self._runtime_profile_paths.pop(session_id)
        try:
            if hasattr(self.gpu_manager, 'unregister_runtime_profile'):
                self.gpu_manager.unregister_runtime_profile(session_id)
        except Exception:
            logger.debug("[Profile] runtime ownership unregister failed", exc_info=True)
        if remove_profile and profile_dir:
            try:
                runtime_root = Path(self.config.profile_base_path) / '.runtime_sessions'
                path = Path(profile_dir).resolve()
                if runtime_root.resolve() in path.parents and path != runtime_root.resolve():
                    await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            except Exception:
                logger.debug("[Profile] runtime profile cleanup failed for %s", session_id, exc_info=True)
        return info
    
    async def load_cookies(self, user_id: str) -> List[Dict]:
        """Load cookies for user - delegates to profile_manager"""
        return await self.profile_manager.load_cookies(user_id)
    
    async def save_cookies(self, user_id: str, cookies: List[Dict]) -> None:
        """Save cookies for user - delegates to profile_manager"""
        await self.profile_manager.save_cookies(user_id, cookies)
    
    def _resolve_launch_mode(self, force_headless: bool = False) -> tuple:
        """
        Live re-resolution of the launch mode at launch time.

        The display state can change after __init__ (Xvfb started later, the
        app sets DISPLAY, RDP session attaches, ...), so every launch path
        must re-check. Returns (mode, display) where mode is one of
        'visible' | 'xvfb' | 'headless'. Starts Xvfb when needed (cached,
        best-effort) and falls back to headless if it cannot start.
        """
        resolution = PlatformRuntime.resolve(force_headless=force_headless)
        mode = resolution['mode']
        display = None
        if mode == PlatformRuntime.MODE_XVFB:
            if self._xvfb.is_linux_headless() and not self._xvfb.start():
                logger.error("[LaunchMode] Xvfb failed to start - falling back to headless mode")
                mode = PlatformRuntime.MODE_HEADLESS
        if mode in (PlatformRuntime.MODE_XVFB, PlatformRuntime.MODE_VISIBLE) \
                and sys.platform.startswith('linux'):
            display = self._xvfb.get_display()
            if display and not os.environ.get('DISPLAY'):
                os.environ['DISPLAY'] = display
        logger.debug(f"[LaunchMode] session launch mode: {mode} ({resolution['reason']})")
        return mode, display

    async def _ensure_xvfb_screen(self) -> None:
        """PCM/session requirement: on a display-less Linux VPS, CREATE a
        virtual X screen (install Xvfb if missing) so Chrome runs HEADED on it
        instead of collapsing to plain headless.

        Xvfb auto-install can take minutes (apt/apk/...), so it NEVER runs on
        the event loop: it is warmed in a daemon thread at BrowserManager
        construction and awaited via an executor here.  One-shot per process.
        """
        if not sys.platform.startswith('linux'):
            return
        if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
            return
        try:
            xvfb = self._xvfb
            if not xvfb.ensure_checked() and not getattr(xvfb, '_install_tried', False):
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    await loop.run_in_executor(None, xvfb.try_install)
                else:
                    xvfb.try_install()
        except Exception as e:
            logger.debug(f"[Xvfb] screen ensure failed (non-fatal): {e}")

    async def _create_browser_direct(
        self,
        session_id: str,
        viewport: Dict,
        pixel_ratio: float,
        user_id: str,
        gpu_id: int,
        user_agent: str,
        is_mobile_client: bool,
        profile_dir: str,
        locale: str = None
    ) -> tuple:
        """
        Create browser using direct Chrome subprocess (no Playwright)
        This method launches Chrome directly and manages it via CDP
        """
        try:
            # Calculate viewport dimensions - the client's CSS pixel viewport
            # is used verbatim (1 CSS px = 1 surface px), floored below.
            logical_width = int(viewport['width'])
            logical_height = int(viewport['height'])
            # FLOOR at Chromium's minimum window width: headed Chrome clamps
            # --window-size to ~500 CSS px wide; a narrower page would sit
            # beside a white strip inside the clamped surface. Filling the
            # window removes the strip at the source (layout == surface).
            if logical_width < MIN_VIEWPORT_WIDTH:
                logger.debug(
                    f"Viewport {logical_width}w floored to Chromium minimum window "
                    f"width {MIN_VIEWPORT_WIDTH} (page fills window - no white strip)")
                logical_width = MIN_VIEWPORT_WIDTH
            is_mobile = is_mobile_client

            # CDP SCREENCAST METHOD (unified for desktop and mobile):
            # Use CDP screencast for BOTH mobile and desktop
            # The browser layout viewport == client CSS pixels exactly.

            viewport_width = logical_width
            viewport_height = logical_height
            
            # Set user agent
            default_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
            final_user_agent = user_agent if user_agent else default_user_agent

            device_preset = match_device_preset(final_user_agent, is_mobile)

            # Determine launch mode LIVE: real display > Xvfb > headless.
            # Never rely on the stale __init__ value (DISPLAY may have
            # appeared since, or Xvfb may have come up later).
            force_headless = bool(getattr(self.config, 'headless', False))
            await self._ensure_xvfb_screen()
            launch_mode, _display = self._resolve_launch_mode(force_headless)
            is_headless = (launch_mode == PlatformRuntime.MODE_HEADLESS)
            logger.debug(f"Direct launch mode: {launch_mode} (headless={is_headless})")

            # LOGICAL-PIXEL POLICY: 1 CSS px = 1 surface px, so the CDP
            # screencast captures exactly the page content - nothing more.
            device_scale_factor = 1.0
            logger.debug(
                f"Logical pixel mode: Layout={viewport_width}x{viewport_height} "
                f"(1 CSS px = 1 surface px)"
            )

            # Launch Chrome directly
            # Outer window height is inflated by the window chrome so the
            # INNER page surface matches the layout height exactly (headed
            # Chrome subtracts title/tab/toolbar from --window-size).
            window_size = f"{viewport_width},{viewport_height + WINDOW_CHROME_HEIGHT}"
            
            result = await DirectChromeLauncher.launch_chrome(
                profile_dir=profile_dir,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                device_scale_factor=device_scale_factor,
                user_agent=final_user_agent,
                is_mobile=is_mobile,
                is_headless=is_headless,
                headless=is_headless,
                window_size=window_size,
                locale=locale
            )
            
            if not result:
                logger.error("Failed to launch Chrome directly")
                return None, None
            
            chrome_process, debug_port, profile_dir = result
            
            # Create CDP connection object (simplified - just holds process info)
            cdp_connection = ChromeCDPConnection(
                chrome_process=chrome_process,
                debug_port=debug_port,
                profile_dir=profile_dir
            )
            await cdp_connection.connect()
            
            # Mobile realism for the direct-CDP path: the Playwright path
            # applies touch emulation + mobile metrics automatically, the
            # direct path must ask Chrome explicitly. Emulation.* commands
            # are PAGE-TARGET domains — previously they were sent over the
            # browser-level websocket and silently rejected, so mobile pages
            # rendered at the wrong width and every site showed a white
            # strip on the right.
            if is_mobile:
                # MOBILE RENDER FIDELITY: also override the emulated screen
                # size from the client's real device metrics so page content
                # renders to the full browser width/height (no white space).
                _swm = viewport_width
                _shm = viewport_height
                try:
                    _swm = int(viewport.get('device_screen_width') or viewport_width)
                    _shm = int(viewport.get('device_screen_height') or viewport_height)
                    # screen must never be smaller than the layout viewport
                    _swm = max(_swm, viewport_width)
                    _shm = max(_shm, viewport_height)
                except Exception:
                    pass
                _metrics = {'width': viewport_width, 'height': viewport_height,
                            'deviceScaleFactor': device_scale_factor, 'mobile': True,
                            'screenWidth': _swm, 'screenHeight': _shm,
                            'screenOrientation': {'type': 'portraitPrimary', 'angle': 0}}
                touch_res = await cdp_connection.send_page_command(
                    'Emulation.setTouchEmulationEnabled',
                    {'enabled': True, 'maxTouchPoints': 5})
                metrics_res = await cdp_connection.send_page_command(
                    'Emulation.setDeviceMetricsOverride', _metrics)
                if not metrics_res:
                    # fallback: browser-level best effort (legacy behavior)
                    logger.warning("[CDP] mobile metrics override failed on page targets — retrying on browser socket")
                    await cdp_connection.send_command(
                        'Emulation.setTouchEmulationEnabled',
                        {'enabled': True, 'maxTouchPoints': 5})
                    await cdp_connection.send_command(
                        'Emulation.setDeviceMetricsOverride', _metrics)
                else:
                    logger.debug(f"[CDP] mobile metrics override applied: {viewport_width}x{viewport_height} "
                                f"screen={_swm}x{_shm}")
            
            # Register session with GPU
            target_gpu = gpu_id if gpu_id is not None else self.gpu_manager.get_gpu_for_session()
            self.gpu_manager.register_session(session_id, target_gpu)
            
            # Store browser info in active browsers (include gpu_id for kickout functionality)
            browser_info = {
                'chrome_process': chrome_process,
                'cdp_connection': cdp_connection,
                'debug_port': debug_port,
                'profile_dir': profile_dir,
                'user_id': user_id,
                'session_id': session_id,
                'gpu_id': target_gpu,
                # Logical-pixel policy: surface == CSS viewport (1x).
                'device_scale_factor': 1.0,
                'mobile_device': (device_preset and next(
                    (k for k, v in DirectChromeLauncher.MOBILE_DEVICES.items()
                     if v is device_preset), None)) or None,
            }
            with self._active_browsers_lock:
                self.active_browsers[session_id] = browser_info
            
            logger.debug(f"Direct Chrome browser created for session {session_id} with debug port {debug_port}")
            
            # Return a mock browser object that mimics Playwright interface
            # This maintains compatibility with existing code
            class DirectChromeBrowser:
                def __init__(self, process, cdp):
                    self.process = process
                    self.cdp = cdp
                    self._closed = False
                
                async def close(self):
                    if not self._closed:
                        try:
                            await self.cdp.close()
                        except Exception:
                            pass
                        self._closed = True
                
                @property
                def closed(self):
                    return self._closed
            
            class DirectChromeContext:
                def __init__(self, browser_info):
                    self.browser_info = browser_info
                    self.user_id = browser_info.get('user_id')
                    self.profile_path = browser_info.get('profile_dir')
                    self._closed = False
                
                async def close(self):
                    if not self._closed:
                        try:
                            self.browser_info['chrome_process'].terminate()
                        except Exception:
                            pass
                        self._closed = True
                
                @property
                def browser(self):
                    return self.browser_info.get('chrome_process')
                
                @property
                def closed(self):
                    return self._closed
            
            browser = DirectChromeBrowser(chrome_process, cdp_connection)
            context = DirectChromeContext(browser_info)
            
            return browser, context
            
        except Exception as e:
            logger.error(f"Error creating direct Chrome browser: {e}")
            return None, None
    
    def _is_google_domain(self, url: str) -> bool:
        """
        Check if the given URL is a Google-related domain.
        Returns True for google.com, google.co.*, and other Google services.
        """
        if not url:
            return False
        
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            hostname = parsed.hostname.lower() if parsed.hostname else ''
            
            # List of Google-related domains
            google_domains = [
                'google.com',
                'google.co.uk',
                'google.co.jp',
                'google.co.kr',
                'google.co.in',
                'google.co.za',
                'google.co.nz',
                'google.co.au',
                'google.co.il',
                'google.co.ve',
                'google.co.th',
                'google.co.id',
                'google.co.ma',
                'google.co.kr',
                'google.com.au',
                'google.com.br',
                'google.com.cn',
                'google.com.co',
                'google.com.de',
                'google.com.ec',
                'google.com.eg',
                'google.com.es',
                'google.com.fr',
                'google.com.gh',
                'google.com.hk',
                'google.com.iq',
                'google.com.kw',
                'google.com.ly',
                'google.com.mx',
                'google.com.my',
                'google.com.ng',
                'google.com.ni',
                'google.com.pk',
                'google.com.pl',
                'google.com.py',
                'google.com.qa',
                'google.com.sa',
                'google.com.sg',
                'google.com.tr',
                'google.com.tw',
                'google.com.ua',
                'google.com.uy',
                'google.com.vn',
                'googleapis.com',
                'googlevideo.com',
                'googleusercontent.com',
                'googlesyndication.com',
                'doubleclick.net',
                'google-analytics.com',
                'googleadservices.com',
                'googlemail.com',
                'gmail.com',
                'youtube.com',
                'ytimg.com',
            ]
            
            # Check if hostname ends with any Google domain
            for domain in google_domains:
                if hostname.endswith(domain) or hostname == domain:
                    return True
            
            return False
        except Exception:
            return False
    
    async def _get_playwright(self):
        """Get or create Playwright instance"""
        async with self._playwright_lock:
            if self._playwright is None:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
            return self._playwright
    
    async def create_browser(self, session_id: str, viewport: Dict,
                             pixel_ratio: float, user_id: str = None,
                             gpu_id: int = None,
                             user_agent: str = None,
                             is_mobile_client: bool = False,
                             target_url: str = None,
                             max_retries: int = 3,
                             retry_delay: float = 2.0,
                             proxy_config: dict = None,
                             locale: str = None) -> tuple:
        """
        Create a new browser instance using real Chrome with persistent profile
        Cross-platform: Windows (visible), Linux (headless with Xvfb)
        Supports mobile emulation via user_agent and viewport parameters
        
        Args:
            target_url: Optional target URL to determine if stealth should be disabled
                       (for Google domains in desktop mode)
            max_retries: Maximum number of retry attempts (default: 3)
            retry_delay: Delay between retries in seconds (default: 2.0)
            proxy_config: Optional proxy configuration dict containing:
                         - proxy_url: Full proxy URL for browser
                         - proxy_host: Proxy hostname
                         - proxy_port: Proxy port
                         - proxy_username: Proxy username
                         - proxy_password: Proxy password
                         - proxy_type: Type of proxy (residential, datacenter, mobile)
            locale: Optional real user locale (e.g. 'de-DE'). When provided it
                    is applied consistently (--lang flag, Accept-Language,
                    navigator.language). When None, NO language is forced -
                    the browser reports the system default, which stays
                    consistent with the rest of the fingerprint.
        """
        # Normalize Apple mobile clients before either the Playwright or direct
        # Chrome path sees the UA.  The mobile flag is forced on for iPhone,
        # iPad, iPod, and iPadOS desktop-mode UAs.
        _apple_mobile = is_apple_mobile_user_agent(user_agent)
        if _apple_mobile:
            user_agent = normalize_mobile_user_agent(user_agent)
            is_mobile_client = True

        # Check if we should disable stealth for Google domains in desktop mode
        # FIX v2 (2026-08-24): removed. Skipping stealth on Google is exactly
        # backwards — the existing _apply_stealth_hardening closes the gaps
        # that earlier versions leaked. We WANT full stealth on Google.
        disable_stealth_google = False
        
        logger.debug(f"=== CREATE BROWSER CALLED ===")
        logger.debug(f"  session_id: {session_id}")
        logger.debug(f"  viewport: {viewport}")
        logger.debug(f"  pixel_ratio: {pixel_ratio}")
        logger.debug(f"  user_id: {user_id}")
        logger.debug(f"  is_mobile_client: {is_mobile_client}")
        logger.debug(f"  user_agent: {user_agent}")
        logger.debug(f"  use_direct_chrome: {self.use_direct_chrome}")
        logger.debug(f"  target_url: {target_url}")
        logger.debug(f"  disable_stealth_google: {disable_stealth_google}")
        logger.debug(f"  max_retries: {max_retries}, retry_delay: {retry_delay}s")
        
        # Initialize variables outside the retry loop
        browser = None
        context = None
        profile_dir = None
        last_error = None
        
        # Retry loop with patient waiting
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    # Wait before retry with progressive backoff
                    wait_time = retry_delay * (attempt + 1)  # Progressive backoff: 4s, 6s, 8s...
                    logger.debug(f"Waiting {wait_time}s before retry attempt {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(wait_time)
                    logger.debug(f"Retry attempt {attempt + 1}/{max_retries} for session {session_id}")
                 
                playwright = await self._get_playwright()
                browser_type = getattr(self.config, 'browser_type', '').lower()
                if browser_type == 'firefox':
                    logger.warning("Firefox browser_type disabled for stability; overriding to chrome")
                    browser_type = 'chrome'
                
                # Determine user ID for profile management
                if not user_id:
                    user_id = session_id
                
                # Keep parent metadata/cookies in the stable profile, but
                # never let a live Chrome process share that directory.
                self.profile_manager.get_or_create_profile(user_id)
                profile_path = self.get_session_profile_path(user_id, session_id)
                profile_dir = str(profile_path)
                
                # Ensure profile directory exists and is writable
                if not os.path.exists(profile_dir):
                    try:
                        os.makedirs(profile_dir, exist_ok=True)
                        logger.debug(f"Created profile directory: {profile_dir}")
                    except Exception as e:
                        logger.error(f"Failed to create profile directory: {profile_dir}, error: {e}")
                
                # Normalize path for Windows (handle paths with spaces)
                # Import platform locally but os is module-level, so this won't affect os scope
                import platform as platform_module
                if platform_module.system() == "Windows":
                    # Ensure path is properly formatted for Windows
                    profile_dir = os.path.normpath(profile_dir)
                    logger.debug(f"Profile path normalized (Windows): {profile_dir}")
                
                # The runtime profile is private to this session.  Never
                # remove another session's Chrome singleton files and never
                # kick a sibling session that shares the durable parent id.
                # A stale runtime directory is safe to reuse only because the
                # SessionManager serializes the same runtime id.

                # ============================================================
                # NEW: Use Direct Chrome Launcher (No Playwright)
                # ============================================================
                if self.use_direct_chrome:
                    logger.debug("=== USING DIRECT CHROME LAUNCHER ===")
                    return await self._create_browser_direct(
                        session_id=session_id,
                        viewport=viewport,
                        pixel_ratio=pixel_ratio,
                        user_id=user_id,
                        gpu_id=gpu_id,
                        user_agent=user_agent,
                        is_mobile_client=is_mobile_client,
                        profile_dir=profile_dir,
                        locale=locale
                    )
                
                # ============================================================
                # Original Playwright-based browser creation
                # ============================================================
                logger.debug("=== USING PLAYWRIGHT BROWSER ===")
                
                # Calculate viewport dimensions - the client's CSS pixel
                # viewport is passed straight to Chromium (1 CSS px = 1
                # surface px for every browser, mobile or desktop), floored.
                logical_width = int(viewport['width'])
                logical_height = int(viewport['height'])
                # FLOOR at Chromium's minimum window width: headed Chrome clamps
                # --window-size to ~500 CSS px wide; a narrower page would sit
                # beside a white strip inside the clamped surface. Filling the
                # window removes the strip at the source (layout == surface).
                if logical_width < MIN_VIEWPORT_WIDTH:
                    logger.debug(
                        f"Viewport {logical_width}w floored to Chromium minimum window "
                        f"width {MIN_VIEWPORT_WIDTH} (page fills window - no white strip)")
                    logical_width = MIN_VIEWPORT_WIDTH

                # Mobile mode is passed directly from session - client knows its device type
                # is_mobile_client: True = mobile device, False = desktop device
                is_mobile = is_mobile_client

                viewport_width = logical_width
                viewport_height = logical_height
                # Set user agent
                default_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36'
                if user_agent:
                    final_user_agent = user_agent
                    logger.debug(f"Using client-provided user agent: {user_agent[:80]}...")
                else:
                    final_user_agent = default_user_agent
                    logger.debug(f"No client user agent provided, using default")

                # Platform-aware mode selection - single source of truth:
                #   real display (Windows desktop/RDP, Linux X11, macOS)
                #   > Xvfb virtual display (HEADED Chrome on a Linux VPS)
                #   > headless (LAST RESORT - strongest bot signal:
                #     outerHeight == innerHeight, no window frame, no screen pos)
                force_headless = bool(getattr(self.config, 'headless', False))
                await self._ensure_xvfb_screen()
                launch_mode, _launch_display = self._resolve_launch_mode(force_headless)
                is_headless = (launch_mode == PlatformRuntime.MODE_HEADLESS)
                # NOTE: Mobile mode does NOT force headless. Mobile sessions
                # run headed everywhere a display or Xvfb exists.

                # LOGICAL-PIXEL POLICY: 1 CSS px = 1 surface px, so the CDP
                # screencast captures exactly the page content - nothing more.
                device_scale_factor = 1.0
                logger.debug(
                    f"Logical pixel mode: Layout={viewport_width}x{viewport_height} "
                    f"(1 CSS px = 1 surface px)"
                )
                
                # Log final browser launch mode
                if is_headless:
                    logger.debug(f"=== BROWSER WILL LAUNCH IN HEADLESS MODE (last resort) ===")
                else:
                    logger.debug(f"=== BROWSER WILL LAUNCH HEADED (mode: {launch_mode}) ===")
                
                browser_type = getattr(self.config, 'browser_type', '').lower()
                if browser_type == 'firefox':
                    logger.warning("Firefox browser_type disabled for stability; overriding to chrome")
                    browser_type = 'chrome'
                # Realistic launch flags - SINGLE SOURCE OF TRUTH
                # (DirectChromeLauncher.build_real_flags). No kiosk, no
                # minimized window, no forced en-US, no bot-signal flags
                # (no-zygote / ipc-flooding / virtual-time / site-isolation
                # off / WebAuthn off / hide-scrollbars).
                browser_args = DirectChromeLauncher.build_real_flags(
                    is_mobile=is_mobile,
                    headless=is_headless,
                    locale=locale,
                    gpu='swiftshader' if launch_mode in (PlatformRuntime.MODE_XVFB, PlatformRuntime.MODE_HEADLESS)
                        else 'auto',
                    visible_window=False,
                )
                if not is_headless:
                    # A normal browser window at the exact viewport. CDP
                    # screencast captures the viewport regardless, so no
                    # kiosk is needed for capture fidelity - and kiosk is a
                    # bot tell (no window frame, outerWidth == innerWidth).
                    browser_args.append(f'--window-size={viewport_width},{viewport_height}')
                    browser_args.append('--window-position=0,0')
                
                # FIX 3: Use System Chrome instead of Playwright Chromium
                # Determine Chrome executable path based on OS
                chrome_executable = None
                if self.config.use_system_chrome:
                    import platform as platform_sys
                    import os as os_sys
                    
                    if platform_sys.system() == "Windows":
                        # Windows Chrome paths
                        possible_paths = [
                            os_sys.environ.get("PROGRAMFILES(X86)") + "\\Google\\Chrome\\Application\\chrome.exe" if os_sys.environ.get("PROGRAMFILES(X86)") else None,
                            os_sys.environ.get("PROGRAMFILES") + "\\Google\\Chrome\\Application\\chrome.exe" if os_sys.environ.get("PROGRAMFILES") else None,
                            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                        ]
                        for path in possible_paths:
                            if path and os_sys.path.exists(path):
                                chrome_executable = path
                                logger.debug(f"Using system Chrome: {chrome_executable}")
                                break
                    elif platform_sys.system() == "Linux":
                        # Linux Chrome path
                        if os_sys.path.exists(self.config.chrome_path):
                            chrome_executable = self.config.chrome_path
                            logger.debug(f"Using system Chrome: {chrome_executable}")
                
                # Create browser with persistent context
                # LOGICAL-PIXEL POLICY: viewport is in CSS pixels only;
                # device_scale_factor fixed at 1.0 (logical-pixel policy).
                launch_kwargs = {
                    'user_data_dir': normalize_path_for_playwright(profile_dir),
                    'headless': is_headless,
                    'args': browser_args,
                    'viewport': {
                        'width': viewport_width,
                        'height': viewport_height
                    },
                    'device_scale_factor': device_scale_factor,
                    'user_agent': final_user_agent,
                    'java_script_enabled': True,
                    # Real browsers HONOR page CSP. bypass_csp=True is a
                    # behavioural bot tell (a page can detect that its own
                    # CSP no longer applies). Injected init scripts still
                    # work - CDP script injection is outside CSP.
                    'bypass_csp': False,
                    'ignore_https_errors': True,
                }
                # Locale: only set when the caller knows the real user's
                # locale. This sets Accept-Language AND navigator.language
                # consistently. Never force en-US server-side.
                if locale:
                    launch_kwargs['locale'] = locale

                # Always pass mobile emulation flags for Chromium.
                # These affect UA / touch / mobile-media-query behavior only -
                # they do NOT resize the viewport - it is already in CSS
                # logical pixels and the surface maps 1:1.
                launch_kwargs['is_mobile'] = is_mobile
                launch_kwargs['has_touch'] = is_mobile

                # MOBILE RENDER FIDELITY: emulate window.screen with the
                # REAL device metrics the client reported. Without this the
                # emulated browser composes the page against a synthetic
                # screen that mismatches the phone, and page content comes
                # out the wrong size with white space inside the browser.
                # Full browser width/height is how the page must render.
                if is_mobile:
                    try:
                        _sw = int(viewport.get('device_screen_width') or viewport_width)
                        _sh = int(viewport.get('device_screen_height') or viewport_height)
                        # screen must never be SMALLER than the layout viewport
                        # (Playwright requires screen >= viewport; a garbage/
                        # collapsed client metric must not shrink the screen).
                        _sw = max(_sw, viewport_width)
                        _sh = max(_sh, viewport_height)
                        if _sw > 0 and _sh > 0:
                            launch_kwargs['screen'] = {'width': _sw, 'height': _sh}
                            logger.debug(f"  - screen emulation: {_sw}x{_sh} (real device metrics)")
                    except Exception:
                        pass

                # Inject Sec-CH-UA (Client Hints) HTTP headers. Many bot-detection
                # services read these headers server-side BEFORE any JS runs, so
                # the JS-side userAgentData spoof alone is not enough.
                #
                # CRITICAL: applies to BOTH mobile and desktop. Desktop especially
                # needs this — otherwise Chromium auto-fills `sec-ch-ua-platform:
                # "Linux"` while the UA says "Windows NT 10.0", an instant red flag
                # for Cloudflare / PerimeterX / FingerprintJS / Yahoo.
                ch_headers = self._build_sec_ch_ua_headers(final_user_agent or '', is_mobile=is_mobile)
                if ch_headers:
                    launch_kwargs['extra_http_headers'] = ch_headers
                    sec_ch_ua_value = ch_headers.get('sec-ch-ua', '') or ''
                    sec_ch_ua_version = '?'
                    if sec_ch_ua_value and 'v="' in sec_ch_ua_value:
                        try:
                            sec_ch_ua_version = sec_ch_ua_value.split('v="')[1].split('"')[0]
                        except Exception:
                            sec_ch_ua_version = '?'

                    logger.debug(
                        f"[CLIENT-HINTS] Injected Sec-CH-UA headers "
                        f"(Chrome/{sec_ch_ua_version}, "
                        f"platform={ch_headers.get('sec-ch-ua-platform', '?')})"
                    )

                # Add proxy configuration if provided (Playwright expects dict format)
                if proxy_config:
                    proxy_url = proxy_config.get('proxy_url') if isinstance(proxy_config, dict) else None
                    if proxy_url:
                        # Parse proxy URL to extract components
                        # Format: http://<username>:<password>@<host>:<port>
                        from urllib.parse import urlparse
                        try:
                            parsed = urlparse(proxy_url)
                            # Extract username and password
                            username = parsed.username or ""
                            password = parsed.password or ""
                            # Build server URL (scheme://host:port)
                            server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}" if parsed.port else f"{parsed.scheme}://{parsed.hostname}"
                            
                            # Pass proxy to Playwright as dict (NOT as --proxy-server arg)
                            launch_kwargs['proxy'] = {
                                'server': server,
                                'username': username,
                                'password': password
                            }
                            logger.debug(f"[PROXY] Full proxy URL: {proxy_url}")
                            logger.debug(f"[PROXY] Server: {server}")
                            logger.debug(f"[PROXY] Username: {username}")
                            logger.debug(f"[PROXY] Password length: {len(password) if password else 0}")
                            logger.debug(f"[PROXY] Proxy dict set in launch_kwargs: {'proxy' in launch_kwargs}")
                        except Exception as e:
                            logger.error(f"[PROXY] Error parsing proxy URL: {e}")
                
                # Debug: Log launch parameters
                logger.debug(f"Launching Playwright browser with:")
                logger.debug(f"  - headless: {is_headless}")
                logger.debug(f"  - viewport: {viewport_width}x{viewport_height} (logical CSS pixels, device_scale_factor=1.0)")
                logger.debug(f"  - is_mobile: {is_mobile}")
                logger.debug(f"  - user_data_dir: {profile_dir}")
                logger.debug(f"  - chrome_executable: {chrome_executable}")
                logger.debug(f"  - browser args count: {len(browser_args)}")
                
                # Additional info for launch mode
                if not is_headless:
                    logger.debug(f"Running in HEADED mode ({launch_mode}) - normal browser window")
                
                # Add executable path if using system Chrome
                if chrome_executable and browser_type != 'firefox':
                    launch_kwargs['executable_path'] = chrome_executable
                    logger.debug(f"Using system Chrome: {chrome_executable}")
                 
                # FIXED: Clean up profile directory locks before launching
                # This prevents "Target closed" errors from stale locks
                import glob as glob_module
                profile_locks = glob_module.glob(os.path.join(profile_dir, "*lock*"))
                for lock_file in profile_locks:
                    try:
                        os.remove(lock_file)
                        logger.debug(f"Removed lock file: {lock_file}")
                    except Exception:
                        pass
                
                # Also remove Singleton* files that can block Chrome launch
                singleton_files = glob_module.glob(os.path.join(profile_dir, "Singleton*"))
                for singleton in singleton_files:
                    try:
                        os.remove(singleton)
                        logger.debug(f"Removed singleton file: {singleton}")
                    except Exception:
                        pass
                
                # FIXED: Deduplicate browser_args to prevent conflicting flags
                browser_args = list(dict.fromkeys(browser_args))
                launch_kwargs['args'] = browser_args

                # Log deduplicated args count
                logger.debug(f"Browser args after deduplication: {len(browser_args)} unique flags")

                # ============================================================
                # SingleFile extension mode
                # ============================================================
                # Path resolution order (first hit wins):
                #   1. SINGLEFILE_EXT_DIR env var (absolute path recommended)
                #   2. <workspace>/single  (workspace = parent of this file)
                #   3. ./single  next to cwd
                #
                # Disable by setting SINGLEFILE_EXT_MODE=0.
                # ============================================================
                _sf_ext_mode = os.environ.get("SINGLEFILE_EXT_MODE", "1").strip() not in (
                    "0", "false", "no", "off"
                )
                if _sf_ext_mode:
                    _sf_ext_dir = os.environ.get("SINGLEFILE_EXT_DIR", "").strip()
                    _sf_candidates: List[str] = []
                    if _sf_ext_dir:
                        _sf_candidates.append(_sf_ext_dir)
                    else:
                        # Default: workspace/single (workspace = dir of this file)
                        _sf_candidates.append(
                            str(Path(__file__).resolve().parent / "single")
                        )
                        # Also accept ./single relative to CWD as a last-ditch fallback
                        _sf_candidates.append(str(Path.cwd() / "single"))
                        # System Chrome may run with different CWD; also check home and absolute
                        _sf_candidates.append(str(Path.home() / "shifixsxs" / "single"))
                        _sf_candidates.append("/home/user/shifixsxs/single")

                    abs_ext: Optional[str] = None
                    for cand in _sf_candidates:
                        cand_abs = os.path.abspath(cand)
                        if os.path.isdir(cand_abs) and (
                            Path(cand_abs) / "manifest.json"
                        ).is_file():
                            abs_ext = cand_abs
                            break

                    if abs_ext is None:
                        logger.warning(
                            f"[SINGLEFILE-EXT] No valid extension dir found "
                            f"(tried {_sf_candidates!r}) — extension NOT loaded"
                        )
                    else:
                        # Check manifest version — Chrome 120+ refuses MV2.
                        try:
                            with open(Path(abs_ext) / "manifest.json", "r", encoding="utf-8") as _mf:
                                _mf_json = json.loads(_mf.read() or "{}")
                                _mf_ver = int(_mf_json.get("manifest_version", 0) or 0)
                        except Exception as _exc:
                            _mf_ver = 0
                            logger.warning(
                                f"[SINGLEFILE-EXT] Could not read manifest: {_exc}"
                            )
                        if _mf_ver < 3:
                            logger.error(
                                f"[SINGLEFILE-EXT] {abs_ext} is manifest_version={_mf_ver} "
                                f"(MV2). Chrome 120+ refuses MV2 — the extension will NOT load. "
                                f"Replace with the MV3 build from "
                                f"https://github.com/gildas-lormeau/SingleFile-MV3"
                            )

                        # Clean stale extension state left by previous placeholder (no key → ephemeral ID fignfifoniblkonapihmkfakmlgkbkcf)
                        # If profile still contains the old ID, Chrome may keep its service_worker (fign...) and not load the new key-derived ID (ebcl...).
                        try:
                            import base64 as _b64c, hashlib as _hlc
                            _manifest_id = None
                            try:
                                with open(Path(abs_ext) / "manifest.json", "r", encoding="utf-8") as _mf2:
                                    _mf2_json = __import__('json').loads(_mf2.read() or "{}")
                                    _key = _mf2_json.get("key") or ""
                                    if _key:
                                        _der = _b64c.b64decode(_key)
                                        _h = _hlc.sha256(_der).digest()
                                        _hx = _h[:16].hex()
                                        _trans = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
                                        _manifest_id = _hx.translate(_trans)
                            except Exception:
                                _manifest_id = None
                            if _manifest_id:
                                # Check Secure Preferences / Preferences for stale ephemeral ID
                                for _pref_name in ["Secure Preferences", "Preferences"]:
                                    _pref_path = Path(profile_dir) / "Default" / _pref_name
                                    if _pref_path.is_file():
                                        try:
                                            _txt = _pref_path.read_text(encoding="utf-8", errors="ignore")
                                            # If file doesn't contain new manifest ID but does contain extensions data, it's stale
                                            if _manifest_id not in _txt and ("fignfifoniblkonapihmkfakmlgkbkcf" in _txt or '"extensions"' in _txt or '"extension"' in _txt.lower()):
                                                # Only delete if it looks like an old profile with extensions
                                                if len(_txt) > 500:
                                                    _pref_path.unlink(missing_ok=True)
                                                    logger.debug(f"[SINGLEFILE-EXT] Cleaned stale {_pref_name} (missing new ID {_manifest_id}, had old data) -> will reload extension")
                                        except Exception:
                                            pass
                                _ext_dir = Path(profile_dir) / "Default" / "Extensions"
                                if _ext_dir.is_dir():
                                    for _child in _ext_dir.iterdir():
                                        if _child.is_dir() and _child.name == "fignfifoniblkonapihmkfakmlgkbkcf" and _child.name != _manifest_id:
                                            try:
                                                import shutil as _shc
                                                _shc.rmtree(_child, ignore_errors=True)
                                                logger.debug(f"[SINGLEFILE-EXT] Removed stale Extensions/{_child.name}")
                                            except Exception:
                                                pass
                        except Exception as _e:
                            logger.debug(f"[SINGLEFILE-EXT] stale cleanup failed: {_e}")

                        # Force the extension to load and block all others.
                        # Strip any prior --disable-extensions / --load-extension
                        # from our flag list so we don't end up with duplicates
                        # or conflicting values.
                        browser_args = [
                            f"--disable-extensions-except={abs_ext}",
                            f"--load-extension={abs_ext}",
                        ] + [
                            a for a in browser_args
                            if not a.startswith("--disable-extensions")
                            and not a.startswith("--disable-extensions-except=")
                            and not a.startswith("--load-extension=")
                        ]
                        launch_kwargs["args"] = browser_args
                        # Strip Playwright's default --disable-extensions which
                        # would otherwise override our --load-extension.
                        launch_kwargs["ignore_default_args"] = list(
                            set(
                                (launch_kwargs.get("ignore_default_args") or [])
                                + ["--disable-extensions", "--disable-component-extensions-with-background-pages"]
                            )
                        )
                        # Force headed when extension present — but ONLY when a
                        # display actually exists.  The previous logic forced
                        # headed even when the runtime had resolved headless
                        # because there is NO X server and NO Xvfb: Chrome then
                        # exits instantly and Playwright reports "Target page,
                        # context or browser has been closed" for every retry
                        # (the PCM browser-never-opens bug on display-less VPSs).
                        # Playwright >= 1.49 runs Chromium's NEW headless mode,
                        # which DOES load MV3 extensions — so headless + ext is
                        # now safe and is the only viable mode without a display.
                        if 'is_headless' in locals() and is_headless:
                            _display_available = launch_mode in (
                                PlatformRuntime.MODE_VISIBLE, PlatformRuntime.MODE_XVFB)
                            if _display_available:
                                logger.warning(f"[SINGLEFILE-EXT] Extension present but session resolved headless ({launch_mode}) — forcing headed; set SINGLEFILE_EXT_MODE=0 to keep headless")
                                try:
                                    launch_kwargs["headless"] = False
                                    browser_args = [a for a in browser_args if a != "--headless=new" and not a.startswith("--headless")]
                                    launch_kwargs["args"] = browser_args
                                except Exception:
                                    pass
                            else:
                                logger.warning("[SINGLEFILE-EXT] no display available — staying headless; new headless mode loads MV3 extensions (Playwright >= 1.49)")
                                launch_kwargs["headless"] = True
                        logger.debug(
                            f"[SINGLEFILE-EXT] Loading unpacked MV{_mf_ver} "
                            f"extension from {abs_ext}"
                        )
                        # System Chrome needs explicit --enable-extensions and no --disable-extensions
                        # Playwright's chromium handles this via ignore_default_args, but system Chrome
                        # may have enterprise policy; ensure we log for debugging
                        if 'chrome_executable' in locals() and chrome_executable:
                            logger.debug(f"[SINGLEFILE-EXT] System Chrome detected, extension will be loaded via --load-extension (bypass_csp=False is intentional for stealth)")
                        logger.debug(f"[SINGLEFILE-EXT] Final launch args include --load-extension={abs_ext} (check chrome://extensions, ID via chrome://version, ignore_default_args={launch_kwargs.get('ignore_default_args')})")


                
                # Launch appropriate Playwright browser (Chromium or Firefox)
                browser_type = getattr(self.config, 'browser_type', '').lower()
                if browser_type == 'firefox':
                    context = await playwright.firefox.launch_persistent_context(**launch_kwargs)
                    context._browser_name = 'firefox'
                else:
                    context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
                    context._browser_name = 'chromium'
                
                # Get browser from context
                # For persistent_context: store context as browser for compatibility
                browser = context

                
                # Apply stealth mode only if enabled in config
                # Skip stealth for Google domains in desktop mode (stealth interferes with Google)
                if self.config.stealth_mode and not disable_stealth_google:
                    logger.debug(f"[STEALTH] Applying stealth mode for session {session_id}")
                    # Get fingerprint for stealth application
                    # Create client_info from available parameters
                    client_info = {
                        'user_agent': user_agent,
                        'viewport': viewport,
                        'pixel_ratio': pixel_ratio,
                        'is_mobile': is_mobile_client
                    }
                    fingerprint = self.fingerprint_manager.get_fingerprint(user_id, client_info)
                    await self._apply_stealth(context, session_id, fingerprint)

                    # Apply WebAuthn/passkey disabling for Microsoft sites
                    await self._apply_webauthn_disable(context, session_id)

                    # Lock floating labels up after first keystroke so they
                    # never animate back down and obscure interactive content.
                    await self._apply_floating_label_lock(context, session_id)

                    # Belt-and-suspenders: rewrite Chromium's user-agent
                    # metadata via CDP so Sec-CH-UA headers agree with
                    # our JS-side userAgentData (closes the gap that
                    # service-worker fetches + early-request scripts
                    # otherwise leak).
                    await self._apply_sec_ch_ua_cdp_override(
                        context, session_id, fingerprint, is_mobile=is_mobile_client
                    )

                    # v2 advanced stealth (2026-08-24): fills gaps in
                    # v1/hardening — Date.toString format, Intl.supportedValuesOf,
                    # canvas toDataURL/measureText, screen.isExtended, etc.
                    try:
                        from stealth_advanced import apply as _apply_v2_stealth
                        await _apply_v2_stealth(context, fingerprint, is_mobile=is_mobile_client)
                    except Exception as _e:
                        logger.warning(f"[STEALTH-V2] desktop apply failed: {_e}")
                else:
                    if disable_stealth_google:
                        logger.debug(f"[STEALTH] Skipping stealth for Google domain in desktop mode for session {session_id}")
                    else:
                        logger.debug(f"[STEALTH] Stealth mode disabled in config for session {session_id}")
                
                # Set up dialog handler
                await self._setup_dialog_handler(context, session_id)
                
                # Apply WebAuthn/passkey disabling for Microsoft sites
                await self._apply_webauthn_disable(context, session_id)
                
                # Register session with GPU
                target_gpu = gpu_id if gpu_id is not None else self.gpu_manager.get_gpu_for_session()
                self.gpu_manager.register_session(session_id, target_gpu)
                
                # Store user_id and profile info
                context.user_id = user_id
                context.profile_path = profile_dir
                
                # Store in active_browsers for session-scoped cleanup.
                with self._active_browsers_lock:
                    self.active_browsers[session_id] = {
                        'browser': browser,
                        'context': context,
                        'user_id': user_id,
                        'session_id': session_id,
                        'profile_dir': profile_dir,
                        'gpu_id': target_gpu,
                        # Logical-pixel policy: surface == CSS viewport (1x).
                        'device_scale_factor': 1.0,
                    }
                
                if attempt > 0:
                    logger.debug(f"Browser created successfully on retry attempt {attempt + 1} for session {session_id}")
                
                return browser, context
                
            except Exception as e:
                last_error = e
                import traceback
                
                if attempt < max_retries - 1:
                    # Retryable error - log warning and continue
                    logger.warning(f"Browser creation attempt {attempt + 1}/{max_retries} failed: {e}")
                    logger.warning(f"Will retry after waiting...")
                    logger.debug(f"Traceback: {traceback.format_exc()}")
                else:
                    # Final attempt failed - log error details
                    logger.error(f"All {max_retries} browser creation attempts failed")
                    logger.error(f"Final error: {e}")
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    
                    # Check common issues
                    if not os.environ.get('DISPLAY') and sys.platform.startswith('linux'):
                        logger.error("DISPLAY environment variable not set - Xvfb may not be running")
                        logger.error("Try running: Xvfb :99 -screen 0 1920x1080x24 &")
                        logger.error("Then: export DISPLAY=:99")
                    
                    # Check if Playwright browsers are installed
                    try:
                        import playwright
                        # FIXED: Use importlib.metadata instead of playwright.__version__
                        try:
                            from importlib.metadata import version as get_version
                            pw_version = get_version("playwright")
                        except Exception:
                            pw_version = "unknown"
                        logger.error(f"Playwright version: {pw_version}")
                        
                        # Check if browsers are installed - use async API instead of sync
                        logger.error("Checking Playwright browser installation...")
                        try:
                            from playwright.async_api import async_playwright
                            # Use async context manager to check browser installation
                            async with async_playwright() as p:
                                await p.chromium.launch(headless=True)
                                logger.error("Playwright browser check: OK")
                        except Exception as browser_check_error:
                            logger.error(f"Playwright browser check failed: {browser_check_error}")
                            logger.error("TIP: Run 'playwright install chromium' to install browser binaries")
                    except Exception as pe:
                        logger.error(f"Playwright error: {pe}")
                        logger.error("TIP: Run 'playwright install chromium' to install browser binaries")
                    
                    # Check profile directory
                    if profile_dir:
                        if not os.path.exists(profile_dir):
                            logger.error(f"Profile directory does not exist: {profile_dir}")
                        elif not os.access(profile_dir, os.W_OK):
                            logger.error(f"Profile directory is not writable: {profile_dir}")
                    
                    # Check Chrome executable
                    if chrome_executable and not os.path.exists(chrome_executable):
                        logger.error(f"Chrome executable not found: {chrome_executable}")
                
                # Cleanup on failed attempt
                try:
                    if 'context' in locals() and context:
                        await context.close()
                except Exception:
                    pass
        
        # All retries exhausted
        logger.error(f"Browser creation failed after {max_retries} attempts")
        return None, None
    
    async def create_browser_simple(self, session_id: str, viewport: Dict,
                                     pixel_ratio: float, user_id: str = None,
                                     gpu_id: int = None,
                                     user_agent: str = None,
                                     is_mobile_client: bool = False,
                                     proxy_config: dict = None,
                                     mobile_device: str = None,
                                     locale: str = None) -> tuple:
        """
        Create a browser for CDP screencast streaming with COMPREHENSIVE STEALTH.
        Uses the SAME stealth configuration as the normal browser to avoid bot detection.
        
        KEY DIFFERENCES from original simple method:
        - NOW uses unified CDP method for BOTH desktop and mobile (not screenshot)
        - NOW runs in NON-HEADLESS mode for mobile to avoid detection
        - NOW applies stealth scripts for maximum anti-detection
        - DEFAULT: Uses mobile device emulation (Pixel 5) for streaming
        
        Args:
            session_id: Session identifier
            viewport: Viewport dict with 'width' and 'height'
            pixel_ratio: Client-reported pixel ratio (informational only;
                the browser surface always maps 1 CSS px = 1 surface px)
            user_id: User identifier
            gpu_id: GPU ID for this session
            user_agent: Custom user agent string
            proxy_config: Optional proxy configuration dict containing:
                         - proxy_url: Full proxy URL for browser
                         - proxy_host: Proxy hostname
                         - proxy_port: Proxy port
                         - proxy_username: Proxy username
                         - proxy_password: Proxy password
                         - proxy_type: Type of proxy (residential, datacenter, mobile)
            is_mobile_client: Whether this is a mobile client
            mobile_device: Mobile device to emulate (default: "pixel_5").
                          Available devices from FingerprintManager.MOBILE_DEVICES:
                          - "iphone_14_pro", "iphone_14", "galaxy_s23", "pixel_7", "pixel_5"
        
        Returns:
            Tuple of (browser, context) or (None, None) on failure
        """
        _apple_mobile = is_apple_mobile_user_agent(user_agent)
        if _apple_mobile:
            user_agent = normalize_mobile_user_agent(user_agent)
            is_mobile_client = True
        try:
            logger.debug("=== CREATING STEALTH BROWSER (for CDP screencast) ===")
            logger.debug("CDP SCREENCAST: Using unified CDP method for both desktop and mobile")
            
            playwright = await self._get_playwright()
            
            if not user_id:
                user_id = session_id
            
            # Parent profile is durable metadata/cookie storage only; the
            # live browser gets its own runtime user-data-dir.
            self.profile_manager.get_or_create_profile(user_id)
            profile_path = self.get_session_profile_path(user_id, session_id)
            profile_dir = str(profile_path)
            
            # NEW: Get or create PERMANENT fingerprint for this user
            # CRITICAL: Fingerprint is generated ONCE and reused forever
            client_info = {
                'user_agent': user_agent,
                'viewport': viewport,
                'pixel_ratio': pixel_ratio,
                'is_mobile': is_mobile_client
            }
            fingerprint = self.fingerprint_manager.get_fingerprint(user_id, client_info)
            logger.debug(f"[Fingerprint] Using permanent fingerprint for {user_id}: {fingerprint.get('device_type', 'unknown')}")
            
            # Calculate viewport dimensions - the client's CSS pixel viewport
            # is used verbatim (1 CSS px = 1 surface px), floored below.
            logical_width = int(viewport.get('width', 360))
            logical_height = int(viewport.get('height', 640))
            # FLOOR at Chromium's minimum window width: headed Chrome clamps
            # --window-size to ~500 CSS px wide; a narrower page would sit
            # beside a white strip inside the clamped surface. Filling the
            # window removes the strip at the source (layout == surface).
            if logical_width < MIN_VIEWPORT_WIDTH:
                logger.debug(
                    f"Viewport {logical_width}w floored to Chromium minimum window "
                    f"width {MIN_VIEWPORT_WIDTH} (page fills window - no white strip)")
                logical_width = MIN_VIEWPORT_WIDTH

            # Mobile detection
            is_mobile = is_mobile_client

            # CDP SCREENCAST METHOD (unified for desktop and mobile):
            # Use CDP screencast for BOTH mobile and desktop
            # The browser layout viewport == client CSS pixels exactly.

            viewport_width = logical_width
            viewport_height = logical_height
            # Get mobile device configuration from FingerprintManager
            # Default: mobile clients keep the historical Pixel 5 streaming
            # default; desktop clients get real desktop emulation instead of
            # being served a Pixel 5 (a desktop client seeing a phone
            # viewport is a mismatch).
            if mobile_device is None:
                mobile_device = 'pixel_5' if is_mobile_client else 'windows_11_chrome'
            mobile_devices = self.fingerprint_manager.MOBILE_DEVICES
            device_config = mobile_devices.get(mobile_device, mobile_devices.get("pixel_5"))
            
            # Device kind follows the preset (mobile presets are phones,
            # desktop presets are real desktops)
            is_mobile = bool(device_config.get('is_mobile', True))


            # Get mobile device properties
            mobile_viewport = device_config.get('viewport', {'width': 393, 'height': 851})
            mobile_ua = normalize_mobile_user_agent(device_config.get('user_agent', '')) or ''
            mobile_platform = (
                'Linux; Android 15'
                if 'Android' in mobile_ua
                else device_config.get('platform', 'Linux; Android 13')
            )
            mobile_has_touch = device_config.get('has_touch', True)

            # Use the mobile device's logical (CSS) viewport as the streaming
            # viewport. The previous code multiplied this by mobile_dsf * 0.70
            # to shrink the capture window for FPS, but under the new
            # logical-pixel policy we keep the device's exact CSS dimensions
            # so the client and server agree on the layout grid.
            mobile_viewport_width = mobile_viewport.get('width', 393)
            mobile_viewport_height = mobile_viewport.get('height', 851)
            viewport_width = mobile_viewport_width
            viewport_height = mobile_viewport_height

            # Use mobile user agent from device configuration
            final_user_agent = mobile_ua

            logger.debug(f"MOBILE DEVICE EMULATION: Using {mobile_device}")
            logger.debug(f"  Device viewport: {mobile_viewport_width}x{mobile_viewport_height} (logical CSS pixels)")
            logger.debug(f"  Streaming viewport: {viewport_width}x{viewport_height}")
            logger.debug(f"  User agent: {final_user_agent[:80]}...")
            logger.debug(f"  Platform: {mobile_platform}")
            logger.debug(f"  Has touch: {mobile_has_touch}")
            
            # CRITICAL: Platform-aware mode selection - single source of
            # truth: real display > Xvfb virtual display (headed) > headless
            # (last resort). A Linux VPS therefore runs a HEADED browser on
            # Xvfb instead of the bot-signaling headless mode.
            force_headless = bool(getattr(self.config, 'headless', False))
            await self._ensure_xvfb_screen()
            launch_mode, _launch_display = self._resolve_launch_mode(force_headless)
            is_headless = (launch_mode == PlatformRuntime.MODE_HEADLESS)
            logger.debug(f"Simple browser launch mode: {launch_mode} (headless={is_headless})")

            # LOGICAL-PIXEL POLICY: 1 CSS px = 1 surface px, so the CDP
            # screencast captures exactly the page content - nothing more.
            device_scale_factor = 1.0
            logger.debug(
                f"Logical pixel mode: Layout={viewport_width}x{viewport_height} "
                f"(1 CSS px = 1 surface px)"
            )
            
            # Realistic launch flags - SINGLE SOURCE OF TRUTH
            # (DirectChromeLauncher.build_real_flags). Same rules as
            # create_browser: no kiosk, no minimized, no forced en-US, no
            # bot-signal flags.
            browser_args = DirectChromeLauncher.build_real_flags(
                is_mobile=True,
                headless=is_headless,
                locale=locale,
                gpu='swiftshader' if launch_mode in (PlatformRuntime.MODE_XVFB, PlatformRuntime.MODE_HEADLESS)
                    else 'auto',
                visible_window=False,
            )
            
            logger.debug(f"CRITICAL: NOT using Playwright is_mobile emulation - using custom mobile stealth only")
            
            # Normal window at the exact viewport (no kiosk) so the CDP
            # screencast captures the viewport AND window geometry looks
            # like a real browser.
            if not is_headless:
                browser_args.append('--window-position=0,0')
                # Inflated OUTER height so the INNER page surface matches the
                # layout height (headed Chrome subtracts window chrome).
                browser_args.append(f'--window-size={viewport_width},{viewport_height + WINDOW_CHROME_HEIGHT}')
            logger.debug(f"WINDOW MODE: viewport={viewport_width}x{viewport_height} (normal window, no kiosk)")
            
            # Log configuration
            logger.debug(f"Launching STEALTH browser with MOBILE DEVICE EMULATION:")
            logger.debug(f"  viewport: {viewport_width}x{viewport_height} (logical CSS pixels, device_scale_factor=1.0)")
            logger.debug(f"  mode: MOBILE ({mobile_device})")
            logger.debug(f"  PLAYWRIGHT mobile emulation: ENABLED")
            logger.debug(f"  JavaScript stealth: APPLYING mobile stealth evasion")
            logger.debug(f"  user_agent: {final_user_agent[:80]}...")
            logger.debug(f"  browser_args count: {len(browser_args)}")
            logger.debug(f"  headless: {is_headless}")
            logger.debug(f"  Mobile stealth: ACTIVE - removing automation markers, applying mobile spoofing")
            
            # FIX: Use System Chrome instead of Playwright Chromium
            # Determine Chrome executable path based on OS
            chrome_executable = None
            if self.config.use_system_chrome:
                import platform as platform_sys
                import os as os_sys
                
                if platform_sys.system() == "Windows":
                    # Windows Chrome paths
                    possible_paths = [
                        os_sys.environ.get("PROGRAMFILES(X86)") + "\\Google\\Chrome\\Application\\chrome.exe" if os_sys.environ.get("PROGRAMFILES(X86)") else None,
                        os_sys.environ.get("PROGRAMFILES") + "\\Google\\Chrome\\Application\\chrome.exe" if os_sys.environ.get("PROGRAMFILES") else None,
                        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                    ]
                    for path in possible_paths:
                        if path and os_sys.path.exists(path):
                            chrome_executable = path
                            logger.debug(f"[MOBILE] Using system Chrome: {chrome_executable}")
                            break
                elif platform_sys.system() == "Linux":
                    # Linux Chrome path
                    if os_sys.path.exists(self.config.chrome_path):
                        chrome_executable = self.config.chrome_path
                        logger.debug(f"[MOBILE] Using system Chrome: {chrome_executable}")
            
            # FIXED: Clean up profile directory locks before launching
            # This prevents "Target closed" errors from stale locks
            import glob as glob_module
            profile_locks = glob_module.glob(os.path.join(profile_dir, "*lock*"))
            for lock_file in profile_locks:
                try:
                    os.remove(lock_file)
                    logger.debug(f"[MOBILE] Removed lock file: {lock_file}")
                except Exception:
                    pass
            
            # Also remove Singleton* files that can block Chrome launch
            singleton_files = glob_module.glob(os.path.join(profile_dir, "Singleton*"))
            for singleton in singleton_files:
                try:
                    os.remove(singleton)
                    logger.debug(f"[MOBILE] Removed singleton file: {singleton}")
                except Exception:
                    pass
            
            # FIXED: Deduplicate browser_args to prevent conflicting flags
            browser_args = list(dict.fromkeys(browser_args))
            
            # Sec-CH-UA (Client Hints) derived from the emulated UA, so the
            # headers always agree with the UA string - for phones AND
            # desktop clients (previously hardcoded to Android/iPhone).
            import re as _re_ch
            _ch_m = _re_ch.search(r'Chrome/(\d+)', final_user_agent or '')
            _ch_ver = _ch_m.group(1) if _ch_m else '147'
            if 'Android' in (final_user_agent or ''):
                _ch_platform, _ch_mobile, _ch_arch, _ch_pver = '"Android"', '?1', '"arm"', '"15.0.0"'
            elif 'iPhone' in (final_user_agent or ''):
                _ch_platform, _ch_mobile, _ch_arch, _ch_pver = '"iOS"', '?1', '"arm"', '"18.3.0"'
            elif 'Windows NT' in (final_user_agent or ''):
                _ch_platform, _ch_mobile, _ch_arch, _ch_pver = '"Windows"', '?0', '"x86"', '"10.0.0"'
            elif 'Macintosh' in (final_user_agent or ''):
                _ch_platform, _ch_mobile, _ch_arch, _ch_pver = '"macOS"', '?0', '"x86"', '"10.15.7"'
            else:
                _ch_platform, _ch_mobile, _ch_arch, _ch_pver = '"Linux"', '?0', '"x86"', '""'
            _ch_model_m = _re_ch.search(r'\((?:iPhone|Linux; Android [^;]+; )([^)]+)\)', final_user_agent or '')
            _ch_model = f'"{_ch_model_m.group(1).strip()}"' if _ch_model_m else '""'
            
            # Create browser context with MOBILE DEVICE EMULATION
            # Use mobile device configuration from FingerprintManager for proper mobile browser behavior
            launch_kwargs = {
                'user_data_dir': normalize_path_for_playwright(profile_dir),
                'headless': is_headless,
                'args': browser_args,
                'viewport': {
                    'width': viewport_width,
                    'height': viewport_height
                },
                'device_scale_factor': device_scale_factor,
                'user_agent': final_user_agent,
                'java_script_enabled': True,
                # Real browsers HONOR page CSP (see create_browser note)
                'bypass_csp': False,
                'ignore_https_errors': True,
                # Locale is set below, only when the caller provides it
                # (never force en-US server-side)
                # MOBILE EMULATION: Enabled with mobile device configuration
                'is_mobile': is_mobile,
                'has_touch': mobile_has_touch,
                # Sec-CH-UA (Client Hints) HTTP headers - sent server-side BEFORE JS runs.
                # This is what real Chrome on Android sends, and is checked by
                # Cloudflare / PerimeterX / DataDome bot detection.
                'extra_http_headers': {
                    'sec-ch-ua': f'"Google Chrome";v="{_ch_ver}", "Chromium";v="{_ch_ver}", "Not_A Brand";v="24"',
                    'sec-ch-ua-mobile': _ch_mobile,
                    'sec-ch-ua-platform': _ch_platform,
                    'sec-ch-ua-platform-version': _ch_pver,
                    'sec-ch-ua-arch': _ch_arch,
                    'sec-ch-ua-bitness': '64',
                    'sec-ch-ua-model': _ch_model,
                    'sec-ch-ua-full-version-list': f'"Google Chrome";v="{_ch_ver}.0.0.0", "Chromium";v="{_ch_ver}.0.0.0", "Not_A Brand";v="24.0.0.0"',
                    # Language follows the real user (locale param), with the
                    # usual browser fallback - never a forced server-side value
                    'accept-language': f'{locale},en;q=0.9' if locale else 'en-US,en;q=0.9',
                },
            }
            if locale:
                launch_kwargs['locale'] = locale
            
            # Add proxy configuration if provided
            # FIXED: Handle proxy URL properly with port fallback and use Playwright's separate fields for credentials
            if proxy_config:
                proxy_url = proxy_config.get('proxy_url')
                if proxy_url:
                    from urllib.parse import urlparse, quote
                    try:
                        parsed = urlparse(proxy_url)
                        
                        # Handle port - use default ports if not specified
                        port = parsed.port
                        if port is None:
                            port = 80 if parsed.scheme == 'http' else 443
                        
                        # Build server URL without credentials
                        server = f"{parsed.scheme}://{parsed.hostname}:{port}"
                        
                        # Extract credentials for Playwright's separate proxy fields
                        username = parsed.username or ""
                        password = parsed.password or ""
                        
                        # Use Playwright's proxy dict with separate server/username/password fields
                        proxy_dict = {'server': server}
                        if username and password:
                            proxy_dict['username'] = username
                            proxy_dict['password'] = password
                        
                        launch_kwargs['proxy'] = proxy_dict
                        logger.debug(f"[PROXY] Using proxy: {parsed.hostname}:{port}")
                        if username:
                            logger.debug(f"[PROXY] Authenticated proxy with username: {username}")
                    except Exception as e:
                        logger.error(f"[PROXY] Error setting proxy: {e}")
            
            # Add executable path if using system Chrome
            if chrome_executable:
                launch_kwargs['executable_path'] = chrome_executable
                logger.debug(f"[DESKTOP] Using system Chrome executable: {chrome_executable}")

            # SingleFile extension for system Chrome (also for Chromium) - ensure visible in Chrome
            # This path previously missed extension loading, so system Chrome never showed it
            _sf_ext_mode = __import__('os').environ.get("SINGLEFILE_EXT_MODE", "1").strip() not in ("0", "false", "no", "off")
            if _sf_ext_mode:
                import os as _os_ext
                _sf_ext_dir = _os_ext.environ.get("SINGLEFILE_EXT_DIR", "").strip()
                _sf_candidates = []
                if _sf_ext_dir:
                    _sf_candidates.append(_sf_ext_dir)
                else:
                    _sf_candidates.append(str(Path(__file__).resolve().parent / "single"))
                    _sf_candidates.append(str(Path.cwd() / "single"))
                    _sf_candidates.append(str(Path.home() / "shifixsxs" / "single"))
                    _sf_candidates.append("/home/user/shifixsxs/single")
                _abs_ext = None
                for cand in _sf_candidates:
                    cand_abs = __import__('os').path.abspath(cand)
                    if __import__('os').path.isdir(cand_abs) and (Path(cand_abs) / "manifest.json").is_file():
                        _abs_ext = cand_abs
                        break
                if _abs_ext is None:
                    import logging as _log
                    _log.getLogger(__name__).warning(f"[SINGLEFILE-EXT/Simple] No valid extension dir found (tried {_sf_candidates!r}) — extension NOT loaded")
                else:
                    try:
                        import json as _json
                        with open(Path(_abs_ext) / "manifest.json", "r", encoding="utf-8") as _mf:
                            _mf_json = _json.loads(_mf.read() or "{}")
                            _mf_ver = int(_mf_json.get("manifest_version", 0) or 0)
                    except Exception:
                        _mf_ver = 0
                    if _mf_ver < 3:
                        import logging as _log2
                        _log2.getLogger(__name__).error(f"[SINGLEFILE-EXT/Simple] {_abs_ext} is MV{_mf_ver} (MV2) Chrome 120+ refuses MV2")
                    # Clean stale extension state (same as above but for simple path)
                    try:
                        import base64 as _b64s, hashlib as _hls
                        _manifest_id_s = None
                        try:
                            with open(Path(_abs_ext) / "manifest.json", "r", encoding="utf-8") as _mf2s:
                                _mf2s_json = __import__('json').loads(_mf2s.read() or "{}")
                                _key_s = _mf2s_json.get("key") or ""
                                if _key_s:
                                    _der_s = _b64s.b64decode(_key_s)
                                    _h_s = _hls.sha256(_der_s).digest()
                                    _hx_s = _h_s[:16].hex()
                                    _trans_s = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
                                    _manifest_id_s = _hx_s.translate(_trans_s)
                        except Exception:
                            _manifest_id_s = None
                        if _manifest_id_s:
                            for _pref_name_s in ["Secure Preferences", "Preferences"]:
                                _pref_path_s = Path(profile_dir) / "Default" / _pref_name_s
                                if _pref_path_s.is_file():
                                    try:
                                        _txt_s = _pref_path_s.read_text(encoding="utf-8", errors="ignore")
                                        if _manifest_id_s not in _txt_s and ("fignfifoniblkonapihmkfakmlgkbkcf" in _txt_s or '"extensions"' in _txt_s):
                                            if len(_txt_s) > 500:
                                                _pref_path_s.unlink(missing_ok=True)
                                                _log2.getLogger(__name__).info(f"[SINGLEFILE-EXT/Simple] Cleaned stale {_pref_name_s} (missing new ID {_manifest_id_s}) -> will reload")
                                    except Exception:
                                        pass
                            _ext_dir_s = Path(profile_dir) / "Default" / "Extensions"
                            if _ext_dir_s.is_dir():
                                for _child_s in _ext_dir_s.iterdir():
                                    if _child_s.is_dir() and _child_s.name == "fignfifoniblkonapihmkfakmlgkbkcf" and _child_s.name != _manifest_id_s:
                                        try:
                                            import shutil as _shs
                                            _shs.rmtree(_child_s, ignore_errors=True)
                                            _log2.getLogger(__name__).info(f"[SINGLEFILE-EXT/Simple] Removed stale Extensions/{_child_s.name}")
                                        except Exception:
                                            pass
                    except Exception as _e_s:
                        _log2.getLogger(__name__).debug(f"[SINGLEFILE-EXT/Simple] stale cleanup failed: {_e_s}")
                    # Force extension load
                    _browser_args = launch_kwargs.get("args", [])
                    _browser_args = [f"--disable-extensions-except={_abs_ext}", f"--load-extension={_abs_ext}"] + [a for a in _browser_args if not a.startswith("--disable-extensions") and not a.startswith("--disable-extensions-except=") and not a.startswith("--load-extension=")]
                    launch_kwargs["args"] = _browser_args
                    launch_kwargs["ignore_default_args"] = list(set((launch_kwargs.get("ignore_default_args") or []) + ["--disable-extensions", "--disable-component-extensions-with-background-pages"]))
                    import logging as _log3
                    if 'is_headless' in locals() and is_headless:
                        _log3.getLogger(__name__).warning(f"[SINGLEFILE-EXT/Simple] Extension present but session was resolved as headless ({launch_mode}) — forcing headed; set SINGLEFILE_EXT_MODE=0 to keep headless")
                        try:
                            launch_kwargs["headless"] = False
                            _browser_args = [a for a in _browser_args if a != "--headless=new" and not a.startswith("--headless")]
                            launch_kwargs["args"] = _browser_args
                        except Exception:
                            pass
                    _log3.getLogger(__name__).info(f"[SINGLEFILE-EXT/Simple] Loading MV{_mf_ver} extension from {_abs_ext} (system Chrome will show it in chrome://extensions)")
                    _log3.getLogger(__name__).info(f"[SINGLEFILE-EXT/Simple] Final launch args include --load-extension={_abs_ext} (check chrome://extensions, ID via chrome://version, ignore_default_args={launch_kwargs.get('ignore_default_args')})")
            
            context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
            
            # FIXED: Verify context launched successfully before proceeding
            # For persistent_context: context IS the browser, not context.browser
            # So we only check if context is None, not context.browser
            if not context:
                logger.error("[DESKTOP] Browser context launched but is None - possible crash")
                return None, None
            
            # Get browser from context
            # For persistent_context: store context as browser for compatibility
            browser = context

            
            # Apply comprehensive stealth mode to avoid bot detection
            # Use MOBILE stealth for mobile device emulation
            if self.config.stealth_mode:
                # Apply mobile stealth for mobile device emulation
                await self._apply_mobile_stealth(context, session_id, fingerprint)

                # Lock floating labels up after first keystroke (mobile sessions
                # in particular rely on the label floating up to type, and
                # Chrome's autofill UI can otherwise animate it back down).
                await self._apply_floating_label_lock(context, session_id)

                # Belt-and-suspenders CDP override of Chromium's
                # user-agent-metadata so Sec-CH-UA headers exactly match
                # our JS-side userAgentData.
                await self._apply_sec_ch_ua_cdp_override(
                    context, session_id, fingerprint, is_mobile=True
                )

                # v2 advanced stealth (2026-08-24, mobile path)
                try:
                    from stealth_advanced import apply as _apply_v2_stealth
                    await _apply_v2_stealth(context, fingerprint, is_mobile=True)
                except Exception as _e:
                    logger.warning(f"[STEALTH-V2] mobile apply failed: {_e}")

            # Set up dialog handler
            await self._setup_dialog_handler(context, session_id)
            
            # Register session with GPU
            target_gpu = gpu_id if gpu_id is not None else self.gpu_manager.get_gpu_for_session()
            self.gpu_manager.register_session(session_id, target_gpu)
            
            # Store user_id and profile info
            context.user_id = user_id
            context.profile_path = profile_dir
            
            # Store in active_browsers
            with self._active_browsers_lock:
                self.active_browsers[session_id] = {
                    'browser': browser,
                    'context': context,
                    'user_id': user_id,
                    'session_id': session_id,
                    'profile_dir': profile_dir,
                    'gpu_id': target_gpu,
                    # Logical-pixel policy: surface == CSS viewport (1x).
                    'device_scale_factor': 1.0,
                }
            
            logger.debug(f"Stealth CDP browser created successfully for session {session_id}")
            return browser, context
            
        except Exception as e:
            import traceback
            logger.error(f"Failed to create stealth desktop browser: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
        
        try:
            if 'context' in locals() and context:
                await context.close()
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return None, None
    
    @staticmethod
    def _build_sec_ch_ua_headers(user_agent: str, is_mobile: bool) -> Dict[str, str]:
        """
        Build the full set of Sec-CH-UA (Client Hints) HTTP headers that real
        Chrome auto-attaches on each navigation. These headers are sent BEFORE
        any JS runs, and Cloudflare/PerimeterX/FingerprintJS/Yahoo read them
        server-side to fingerprint the browser.

        Without these headers, Chromium falls back to its OWN values which are
        derived from the actual host platform — on a Linux server they would
        be `sec-ch-ua-platform: "Linux"` regardless of what we spoof in JS,
        instantly contradicting a "Windows NT 10.0" UA string.

        Args:
            user_agent: The (possibly spoofed) User-Agent string we're sending.
            is_mobile:  Whether this is a mobile context.

        Returns:
            Dict of headers ready to be passed as `extra_http_headers`.
        """
        import re
        if not user_agent:
            return {}

        # Extract the real Chrome major version from the UA string so every
        # client-hint version field matches the UA. Hardcoding "147" here
        # would mismatch a UA like "Chrome/123" — also a fingerprint signal.
        ch_chrome_version = '147'
        m = re.search(r'Chrome/(\d+)', user_agent)
        if m:
            ch_chrome_version = m.group(1)

        is_android = 'Android' in user_agent
        is_ios = 'iPhone' in user_agent or 'iPad' in user_agent or 'iOS' in user_agent

        if is_mobile:
            # Mobile context — derive platform chip accordingly
            sec_ch_ua_platform = 'Android' if is_android else 'iOS'
            sec_ch_ua_platform_version = '15.0.0' if is_android else '18.3.1'
            sec_ch_ua_arch = 'arm'
            sec_ch_ua_mobile = '?1'
            if 'Pixel' in user_agent:
                sec_ch_ua_model = 'Pixel'
            elif 'SM-S' in user_agent:
                sec_ch_ua_model = 'SM-S'
            elif 'iPhone' in user_agent:
                sec_ch_ua_model = 'iPhone'
            else:
                sec_ch_ua_model = ''
        else:
            # Desktop context — pin to platform from the UA token
            if 'Windows' in user_agent:
                sec_ch_ua_platform = 'Windows'
                sec_ch_ua_platform_version = '15.0.0'  # current Win 11 build
                sec_ch_ua_arch = 'x86'
            elif 'Mac' in user_agent or 'Macintosh' in user_agent:
                sec_ch_ua_platform = 'macOS'
                sec_ch_ua_platform_version = '14.5.1'
                sec_ch_ua_arch = 'arm'
            elif 'Linux' in user_agent:
                sec_ch_ua_platform = 'Linux'
                sec_ch_ua_platform_version = '6.5.0'
                sec_ch_ua_arch = 'x86'
            else:
                sec_ch_ua_platform = 'Windows'
                sec_ch_ua_platform_version = '15.0.0'
                sec_ch_ua_arch = 'x86'
            sec_ch_ua_mobile = '?0'
            sec_ch_ua_model = ''

        return {
            'sec-ch-ua': (
                f'"Google Chrome";v="{ch_chrome_version}", '
                f'"Chromium";v="{ch_chrome_version}", '
                f'"Not_A Brand";v="24"'
            ),
            'sec-ch-ua-mobile': sec_ch_ua_mobile,
            'sec-ch-ua-platform': f'"{sec_ch_ua_platform}"',
            'sec-ch-ua-platform-version': f'"{sec_ch_ua_platform_version}"',
            'sec-ch-ua-arch': f'"{sec_ch_ua_arch}"',
            'sec-ch-ua-bitness': '"64"',
            'sec-ch-ua-model': f'"{sec_ch_ua_model}"',
            'sec-ch-ua-full-version-list': (
                f'"Google Chrome";v="{ch_chrome_version}.0.0.0", '
                f'"Chromium";v="{ch_chrome_version}.0.0.0", '
                f'"Not_A Brand";v="24.0.0.0"'
            ),
            'accept-language': 'en-US,en;q=0.9',
        }

    async def _apply_stealth_hardening(self, context, session_id: str,
                                       fingerprint: Dict = None,
                                       is_mobile: bool = False):
        """
        HARDENED stealth extras. Patches gaps that the basic stealth scripts
        miss but that bot-detection services (Cloudflare, PerimeterX,
        FingerprintJS Pro, Yahoo, Microsoft) actively check:

          * window.outerWidth / outerHeight (headless returns 0 / equals
            innerWidth — a classic headless leak)
          * Notification.permission (headless returns "denied"; real Chrome
            returns "default" on a clean profile)
          * screen.colorDepth / pixelDepth (headless returns 24 but
            inconsistent with Windows profiles that report 30 / 48)
          * Error.prepareStackTrace / Error.captureStackTrace scrubbing
            (Playwright paths leak in `new Error().stack`)
          * Symbol.toStringTag on `navigator.plugins` /
            `navigator.mimeTypes` (real Chrome has them set)
          * document.hasFocus() — return based on a deterministic timer to
            break a one-shot deterministic-true check
          * RTCPeerConnection.createOffer leak prevention
            (overrides ICE candidate parsing to drop IP-revealing lines)
          * Performance.measureUserAgentSpecificMemory() returns a stable
            random number tied to the fingerprint seed
          * Date.prototype.toString and Date.parse consistency with the
            fingerprint timezone so `new Date().toString()` matches
            `Intl.DateTimeFormat().resolvedOptions().timeZone`
          * chrome.runtime.id rotated to a syntactically-valid 32-char
            lowercase extension ID with `chrome.runtime.getURL()` updated
            accordingly (Cloudflare catalogs known fake IDs)

        All values are derived from the fingerprint so a user's profile
        stays self-consistent across sessions (the whole point of
        fingerprint persistence).
        """
        if context is None:
            return

        if fingerprint is None:
            fingerprint = {}

        fp = {
            'timezone': fingerprint.get('timezone', 'America/New_York'),
            'timezone_offset': fingerprint.get('timezone_offset', -300),
            'language': fingerprint.get('language', 'en-US'),
            'platform': fingerprint.get('platform', 'Win32'),
            'oscpu': fingerprint.get('oscpu', 'Windows NT 10.0; Win64; x64'),
            'cpu_cores': fingerprint.get('cpu_cores', 8),
            'memory': fingerprint.get('memory', 16),
            'webgl_vendor': fingerprint.get('webgl_vendor',
                                            'Google Inc. (NVIDIA)'),
            'webgl_renderer': fingerprint.get('webgl_renderer',
                                              'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0)'),
            'fonts': fingerprint.get('fonts',
                                      ['Segoe UI', 'Arial', 'Times New Roman']),
            'is_mobile': bool(is_mobile),
            'has_touch': bool(is_mobile),
            'max_touch_points': fingerprint.get('max_touch_points',
                                                5 if is_mobile else 0),
            'canvas_seed': fingerprint.get('canvas_seed', 12345),
            'audio_seed': fingerprint.get('audio_seed', 12345),
            'screen_width': fingerprint.get('screen_width', 1920),
            'screen_height': fingerprint.get('screen_height', 1080),
        }

        # Derive a "real-looking" extension ID from the canvas seed so it
        # stays self-consistent per profile — the same user always sees the
        # same chrome.runtime.id across sessions.
        import hashlib
        seed_str = str(fp['canvas_seed'])
        runtime_id = hashlib.md5(seed_str.encode()).hexdigest()[:32]
        runtime_id_url = (
            'chrome-extension://' + runtime_id + '/{path}'
        )

        # Pull Chrome version from the user-agent for userAgentData patching
        import re as _re
        ua_str = fingerprint.get('user_agent', '') or ''
        chrome_version = '147'
        m = _re.search(r'Chrome/(\d+)', ua_str)
        if m:
            chrome_version = m.group(1)

        # The Sec-CH-UA "high-entropy" version list uses dotted versions,
        # so we need a minor/patch string. Use 147.0.0.0 as a safe default
        # — modern Chrome deliberately advertises this padded version.
        full_version = f"{chrome_version}.0.0.0"
        ua_chrome_version_major = chrome_version

        is_android_ua = 'Android' in ua_str
        is_ios_ua = ('iPhone' in ua_str or 'iPad' in ua_str)
        if is_mobile:
            ua_data_platform = (
                'Android' if is_android_ua
                else ('iOS' if is_ios_ua else 'Android')
            )
            platform_version = (
                '15.0.0.0' if is_android_ua else '18.3.1.0'
            )
            architecture = '' if is_ios_ua else 'arm'
            ua_full_version = full_version
            ua_full_version_list = [
                {'brand': 'Chromium', 'version': full_version},
                {'brand': 'Google Chrome', 'version': full_version},
                {'brand': 'Not_A Brand', 'version': '24.0.0.0'},
            ]
        else:
            ua_data_platform = (
                'Windows' if 'Windows' in fp['platform']
                else ('macOS' if 'Mac' in fp['platform'] or 'Intel' in fp['platform']
                      else 'Linux')
            )
            platform_version = (
                '15.0.0.0' if ua_data_platform == 'Windows'
                else ('14.5.1.0' if ua_data_platform == 'macOS'
                      else '6.5.0.0')
            )
            architecture = (
                'arm' if ua_data_platform == 'macOS'
                else 'x86'
            )
            ua_full_version = full_version
            ua_full_version_list = [
                {'brand': 'Chromium', 'version': full_version},
                {'brand': 'Google Chrome', 'version': full_version},
                {'brand': 'Not_A Brand', 'version': '24.0.0.0'},
            ]

        # Bracket characters the way Python f-strings won't choke on the
        # CSS-string escape rules of the file.
        script = rf"""
        (function() {{
            'use strict';
            if (window.__stealthHardeningV1) return;
            window.__stealthHardeningV1 = true;

            const FP = {json.dumps(fp)};
            const RUNTIME_ID = '{runtime_id}';
            const RUNTIME_ID_URL = 'chrome-extension://' + RUNTIME_ID + '/';
            const CHROME_MAJOR = '{ua_chrome_version_major}';
            const FULL_VERSION = '{ua_full_version}';
            const UA_DATA_PLATFORM = '{ua_data_platform}';
            const UA_PLATFORM_VERSION = '{platform_version}';
            const UA_ARCH = '{architecture}';
            const IS_MOBILE = {str(is_mobile).lower()};

            // ===== A. chrome.runtime.id with a deterministic, valid-shaped ID
            try {{
                if (window.chrome && window.chrome.runtime) {{
                    Object.defineProperty(window.chrome.runtime, 'id', {{
                        get: () => RUNTIME_ID,
                        configurable: true
                    }});
                    const origGetURL = window.chrome.runtime.getURL;
                    window.chrome.runtime.getURL = function(path) {{
                        try {{
                            return RUNTIME_ID_URL + (path || '');
                        }} catch (e) {{ return origGetURL ? origGetURL(path) : RUNTIME_ID_URL; }}
                    }};
                }}
            }} catch (e) {{}}

            // ===== B. window.outerWidth / outerHeight: headless returns 0
            // or identical to innerWidth. Real Chrome always has OS chrome
            // borders. We use deterministic numbers per platform.
            try {{
                const _wrap = window;
                const _innerW = _wrap.innerWidth;
                const _innerH = _wrap.innerHeight;
                const _outerChromeW = IS_MOBILE ? 0 : 8;   // side borders
                const _outerChromeH = IS_MOBILE ? 0 : 90;  // title bar + tab strip
                const _ow = _innerW + _outerChromeW;
                const _oh = _innerH + _outerChromeH;
                Object.defineProperty(_wrap, 'outerWidth', {{
                    get: () => _ow,
                    configurable: true
                }});
                Object.defineProperty(_wrap, 'outerHeight', {{
                    get: () => _oh,
                    configurable: true
                }});
                // Some UAs expose outerWidth only via window.screen; mirror.
                try {{
                    Object.defineProperty(_wrap.screen, 'width', {{
                        get: () => FP.screen_width || _ow,
                        configurable: true
                    }});
                    Object.defineProperty(_wrap.screen, 'height', {{
                        get: () => FP.screen_height || (_oh + 40),
                        configurable: true
                    }});
                }} catch (e) {{}}
            }} catch (e) {{}}

            // ===== C. Notification.permission (headless returns "denied",
            // real Chrome returns "default" until a site asks).
            try {{
                if (typeof Notification !== 'undefined') {{
                    const realGetter = Object.getOwnPropertyDescriptor(Notification, 'permission');
                    Object.defineProperty(Notification, 'permission', {{
                        get: () => 'default',
                        configurable: true
                    }});
                    // Wrap requestPermission so it returns 'default' until
                    // the user "grants" — headless returns denied instantly.
                    if (Notification.requestPermission) {{
                        const origRP = Notification.requestPermission.bind(Notification);
                        Notification.requestPermission = function(cb) {{
                            try {{
                                const p = Promise.resolve('default');
                                if (cb) p.then(cb);
                                return p;
                            }} catch (e) {{ return origRP(cb); }}
                        }};
                    }}
                }}
            }} catch (e) {{}}

            // ===== D. screen.colorDepth / pixelDepth — fingerprint.com.au
            // checks these. Default 24 is fine for most platforms but in
            // Windows HDR profiles it's 30 / 48.
            try {{
                const _cd = IS_MOBILE ? 24 : 24;
                Object.defineProperty(screen, 'colorDepth', {{
                    get: () => _cd, configurable: true
                }});
                Object.defineProperty(screen, 'pixelDepth', {{
                    get: () => _cd, configurable: true
                }});
            }} catch (e) {{}}

            // ===== E. document.hasFocus() — headless always returns true
            // for the only visible page. Real Chrome returns false when the
            // tab loses focus. We toggle based on a deterministic timer so
            // it isn't ALWAYS true (this is enough to defeat single-shot
            // detection scripts that check once at load time).
            try {{
                const _t0 = Date.now();
                const _origHasFocus = document.hasFocus;
                document.hasFocus = function() {{
                    try {{
                        const _dt = (Date.now() - _t0) % 13000;
                        // In the first 5s after load, report true (matches
                        // real Chrome). After that, flip to a deterministic
                        // pattern that is NOT always-true.
                        return _dt < 5000;
                    }} catch (e) {{ return _origHasFocus.call(document); }}
                }};
            }} catch (e) {{}}

            // ===== F. Error.prepareStackTrace / Error.captureStackTrace
            // scrubbing so Playwright internal paths ("@playwright/lib/...")
            // don't appear in stack traces that bot-detection reads via
            // .stack.
            try {{
                const _origCapture = Error.captureStackTrace;
                Error.captureStackTrace = function(target, fn) {{
                    try {{
                        if (_origCapture) {{
                            _origCapture.call(this, target, fn);
                        }}
                        if (target && target.stack) {{
                            target.stack = String(target.stack)
                                .split('\n')
                                .filter(function(line) {{
                                    return !/playwright|puppeteer|selenium|webdriver|chromedriver/i.test(line);
                                }}).join('\n');
                        }}
                    }} catch (e) {{}}
                }};
                const _origPrep = Error.prepareStackTrace;
                Error.prepareStackTrace = function(err, frames) {{
                    try {{
                        const filtered = (frames || []).filter(function(f) {{
                            const fStr = String((f && (f.toString && f.toString())) || '');
                            return !/playwright|puppeteer|selenium|webdriver|chromedriver/i.test(fStr);
                        }});
                        const out = _origPrep ? _origPrep.call(Error, err, filtered) : null;
                        return out || filtered.map(function(f) {{
                            return '    at ' + (f.toString ? f.toString() : '?');
                        }}).join('\n');
                    }} catch (e) {{
                        return _origPrep ? _origPrep.call(Error, err, frames) : (frames || []).map(function(f) {{
                            return '    at ' + (f && f.toString ? f.toString() : '?');
                        }}).join('\n');
                    }}
                }};
            }} catch (e) {{}}

            // ===== G. Symbol.toStringTag on navigator.plugins / mimeTypes
            // — real Chrome sets them to 'PluginArray' / 'MimeTypeArray'.
            try {{
                if (typeof Symbol === 'object' && Symbol.toStringTag) {{
                    if (navigator.plugins) {{
                        try {{ Object.defineProperty(HTMLCollection.prototype, Symbol.toStringTag, {{ get: () => 'PluginArray', configurable: true }}); }} catch(e) {{}}
                        try {{ Object.defineProperty(navigator.plugins.__proto__, Symbol.toStringTag, {{ get: () => 'PluginArray', configurable: true }}); }} catch(e) {{}}
                    }}
                    if (navigator.mimeTypes) {{
                        try {{ Object.defineProperty(MimeTypeArray.prototype, Symbol.toStringTag, {{ get: () => 'MimeTypeArray', configurable: true }}); }} catch(e) {{}}
                        try {{ Object.defineProperty(navigator.mimeTypes.__proto__, Symbol.toStringTag, {{ get: () => 'MimeTypeArray', configurable: true }}); }} catch(e) {{}}
                    }}
                }}
            }} catch (e) {{}}

            // ===== H. RTCPeerConnection.createOffer — strip IP addresses
            // from SDP candidates so WebRTC fingerprinters can't read them.
            try {{
                const _OrigRTC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
                if (_OrigRTC && !_OrigRTC.__hardened) {{
                    const _Wrapped = function() {{
                        const pc = new _OrigRTC(...arguments);
                        const _origCO = pc.createOffer.bind(pc);
                        pc.createOffer = async function() {{
                            const offer = await _origCO(...arguments);
                            try {{
                                if (offer && offer.sdp) {{
                                    offer.sdp = offer.sdp.replace(
                                        /a=candidate:.*typ (srtyp|host).*\r?\n/g, ''
                                    );
                                }}
                            }} catch (e) {{}}
                            return offer;
                        }};
                        const _origCA = pc.createAnswer.bind(pc);
                        pc.createAnswer = async function() {{
                            const answer = await _origCA(...arguments);
                            try {{
                                if (answer && answer.sdp) {{
                                    answer.sdp = answer.sdp.replace(
                                        /a=candidate:.*typ (srtyp|host).*\r?\n/g, ''
                                    );
                                }}
                            }} catch (e) {{}}
                            return answer;
                        }};
                        return pc;
                    }};
                    _Wrapped.__hardened = true;
                    _Wrapped.prototype = _OrigRTC.prototype;
                    window.RTCPeerConnection = _Wrapped;
                    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = _Wrapped;
                }}
            }} catch (e) {{}}

            // ===== I. Performance.measureUserAgentSpecificMemory() returns
            // a stable value derived from the fingerprint seed instead of
            // a fresh per-call allocation profile (which exposes heap
            // patterns).
            try {{
                if (performance && performance.measureUserAgentSpecificMemory) {{
                    const _origMUASM = performance.measureUserAgentSpecificMemory.bind(performance);
                    performance.measureUserAgentSpecificMemory = async function() {{
                        try {{
                            const baseBytes = 16777216 + ((FP.canvas_seed || 1) % 134217728);
                            return {{
                                bytes: baseBytes,
                                breakdown: [
                                    {{ bytes: Math.floor(baseBytes * 0.4), attribution: [], scope: 'Window' }},
                                    {{ bytes: Math.floor(baseBytes * 0.6), attribution: [], scope: 'Window' }},
                                ]
                            }};
                        }} catch (e) {{ return _origMUASM(); }}
                    }};
                }}
            }} catch (e) {{}}

            // ===== J. Date.prototype.toString should match the fingerprint
            // timezone exactly. Without this, sites that grep
            // `GMT+0000` vs `Intl.DateTimeFormat().resolvedOptions().timeZone`
            // see a mismatch.
            try {{
                const _OFF = FP.timezone_offset || 0;
                const _TZ = FP.timezone || 'UTC';
                const _origToString = Date.prototype.toString;
                Date.prototype.toString = function() {{
                    try {{
                        const _ms = this.getTime();
                        const _tzSign = _OFF > 0 ? '-' : '+';
                        const _tzAbs = Math.abs(_OFF);
                        const _tzh = String(Math.floor(_tzAbs / 60)).padStart(2, '0');
                        const _tzm = String(_tzAbs % 60).padStart(2, '0');
                        return (
                            this.toDateString() +
                            ' ' +
                            this.toTimeString().split(' ')[0] +
                            ' GMT' + _tzSign + _tzh + _tzm +
                            ('(' + _TZ + ')')
                        );
                    }} catch (e) {{ return _origToString.call(this); }}
                }};
            }} catch (e) {{}}

            // ===== K. navigator.userAgentData — must use the SAME Chrome
            // major version as the rest of the client hints; this is the
            // single biggest "instant red flag" for FingerprintJS Pro
            // when version numbers don't match.
            try {{
                const _uList = {json.dumps(ua_full_version_list)};
                const _uData = {{
                    brands: _uList.slice(0, 2),
                    mobile: IS_MOBILE,
                    platform: UA_DATA_PLATFORM,
                    getHighEntropyValues: function() {{
                        return Promise.resolve({{
                            architecture: UA_ARCH,
                            bitness: '64',
                            brands: _uList,
                            mobile: IS_MOBILE,
                            model: '',
                            platform: UA_DATA_PLATFORM,
                            platformVersion: UA_PLATFORM_VERSION,
                            uaFullVersion: FULL_VERSION,
                            fullVersionList: _uList,
                            wow64: false
                        }});
                    }},
                    toJSON: function() {{
                        return {{ brands: this.brands, mobile: this.mobile, platform: this.platform }};
                    }}
                }};
                // Replace the property descriptor entirely so even
                // toString/proxy checks see our version.
                Object.defineProperty(Navigator.prototype, 'userAgentData', {{
                    get: function() {{ return _uData; }},
                    configurable: true,
                    enumerable: true
                }});
                // Some plugins or scripts cache the userAgentData object
                // before our override; patch the constructor too.
                try {{
                    Object.defineProperty(window.Navigator, 'prototype', {{
                        writable: false, configurable: false
                    }});
                }} catch (e) {{}}
            }} catch (e) {{}}

            // ===== L. Performance.memory shape (Chrome-only). Some
            // fingerprinters hash a uniform jsHeapSizeLimit.
            try {{
                if (performance && 'memory' in performance) {{
                    Object.defineProperty(performance, 'memory', {{
                        get: () => ({{
                            jsHeapSizeLimit: 4294967296,
                            totalJSHeapSize: 30000000 + ((FP.canvas_seed || 1) % 5000000),
                            usedJSHeapSize:  15000000 + ((FP.canvas_seed || 1) % 4000000),
                        }}),
                        configurable: true
                    }});
                }}
            }} catch (e) {{}}

            console.debug('[STEALTH-HARDENING] v1 applied (UA=' + UA_DATA_PLATFORM + ' v' + CHROME_MAJOR + ', mobile=' + IS_MOBILE + ')');
        }})();
        """

        try:
            await context.add_init_script(script)
            logger.debug(
                f"[STEALTH-HARDENING] Applied v1 for session {session_id} "
                f"(platform={ua_data_platform}, chrome={ua_chrome_version_major})"
            )
        except Exception as e:
            logger.warning(
                f"[STEALTH-HARDENING] Failed to apply for session {session_id}: {e}"
            )

    async def _apply_sec_ch_ua_cdp_override(self, context, session_id: str,
                                            fingerprint: Dict = None,
                                            is_mobile: bool = False):
        """
        Belt-and-suspenders: beyond `extra_http_headers` (added at launch
        time) and the JS-side `navigator.userAgentData` patch, also tell
        Chromium's networking layer to use our spoofed User-Agent metadata
        via CDP.

        This rewrites the values Chromium's internal `BrandVersionService`
        returns to the `sec-ch-ua*` HTTP header generator. Without this,
        Chromium can still leak the real User-Agent metadata into
        Client Hint headers on some sites if the request bypasses
        `extra_http_headers` (e.g. service-worker-driven fetches).

        Behavior:
            * Tries to open a CDP session against the first page.
            * Sends `Network.setUserAgentMetadata` with our brand list.
            * Skips silently on any error (CDP method is best-effort).
        """
        if context is None:
            return
        try:
            page = None
            # Prefer an existing page so we don't have to spawn one.
            pages = getattr(context, 'pages', None) or []
            if pages:
                page = pages[0]
            if page is None:
                return

            cdp = await context.new_cdp_session(page)
            try:
                fp = fingerprint or {}
                ua = fp.get('user_agent', '') or ''
                import re as _re
                major = '147'
                m = _re.search(r'Chrome/(\d+)', ua)
                if m:
                    major = m.group(1)
                full_version = major + '.0.0.0'
                platform_token = fp.get('platform', '') or ''
                pt_lower = platform_token.lower()
                if is_mobile:
                    ua_platform = 'Android' if 'android' in ua.lower() else 'iOS'
                elif 'mac' in pt_lower or 'intel' in pt_lower:
                    ua_platform = 'macOS'
                elif 'linux' in pt_lower:
                    ua_platform = 'Linux'
                else:
                    ua_platform = 'Windows'
                if ua_platform == 'Android':
                    platform_version = '15.0.0.0'
                    architecture = 'arm'
                elif ua_platform == 'iOS':
                    platform_version = '18.3.1.0'
                    architecture = ''
                elif ua_platform == 'macOS':
                    platform_version = '14.5.1.0'
                    architecture = 'arm'
                elif ua_platform == 'Linux':
                    platform_version = '6.5.0.0'
                    architecture = 'x86'
                else:
                    platform_version = '15.0.0.0'
                    architecture = 'x86'

                brand_list = [
                    {'brand': 'Not_A Brand', 'version': '24.0.0.0'},
                    {'brand': 'Google Chrome', 'version': full_version},
                    {'brand': 'Chromium', 'version': full_version},
                ]

                await cdp.send("Network.setUserAgentMetadata", {
                    'userAgent': ua,
                    'acceptLanguage': fp.get('language', 'en-US,en;q=0.9'),
                    'platform': ua_platform,
                    'userAgentMetadata': {
                        'brands': brand_list[:2],
                        'fullVersionList': brand_list,
                        'platform': ua_platform,
                        'platformVersion': platform_version,
                        'architecture': architecture,
                        'model': '',
                        'mobile': bool(is_mobile),
                        'bitness': '64',
                        'wow64': False,
                    },
                })
                # Also set the UA override (applies to JavaScript
                # navigator.userAgent AND every outgoing request line).
                await cdp.send("Network.setUserAgentOverride", {
                    'userAgent': ua,
                    'acceptLanguage': fp.get('language', 'en-US,en;q=0.9'),
                    'platform': ua_platform,
                    'userAgentMetadata': {
                        'brands': brand_list[:2],
                        'fullVersionList': brand_list,
                        'platform': ua_platform,
                        'platformVersion': platform_version,
                        'architecture': architecture,
                        'model': '',
                        'mobile': bool(is_mobile),
                        'bitness': '64',
                        'wow64': False,
                    },
                })
                logger.debug(
                    f"[SEC-CH-UA-CDP] Network.setUserAgentMetadata applied "
                    f"for session {session_id} (platform={ua_platform}, "
                    f"chrome={major}, mobile={is_mobile})"
                )
            finally:
                try:
                    await cdp.detach()
                except Exception:
                    pass
        except Exception as e:
            logger.debug(
                f"[SEC-CH-UA-CDP] CDP override skipped for session "
                f"{session_id}: {e}"
            )

    async def _apply_stealth(self, context, session_id: str, fingerprint: Dict = None):
        """
        Apply comprehensive stealth mode using Playwright-Stealth + custom advanced evasion scripts.
        Uses fingerprint data for consistent spoofing across sessions.
        
        Enhanced evasion coverage:
        - Core automation flags removal
        - CDP/Playwright artifact elimination
        - Canvas fingerprint spoofing with noise injection
        - WebGL renderer spoofing (from fingerprint)
        - Audio context fingerprint prevention
        - Navigator properties spoofing (from fingerprint)
        - Permissions API normalization
        - Timezone spoofing (from fingerprint - matches proxy location)
        - Hardware concurrency (from fingerprint)
        - Plugin enumeration spoofing
        - Chrome runtime spoofing
        - Client hints spoofing
        - Connection API spoofing
        - Battery API mocking
        - Media devices spoofing
        - Speech synthesis spoofing
        - WebRTC leak prevention
        
        IMPORTANT: Uses fingerprint values for consistency - generated ONCE, reused forever
        """
        
        # Get fingerprint with defaults
        if fingerprint is None:
            fingerprint = {}
        
        # Extract fingerprint values with defaults
        fp = {
            'timezone': fingerprint.get('timezone', 'America/New_York'),
            'language': fingerprint.get('language', 'en-US'),
            'languages': fingerprint.get('languages', ['en-US', 'en']),
            'country': fingerprint.get('country', 'US'),
            'platform': fingerprint.get('platform', 'Win32'),
            'oscpu': fingerprint.get('oscpu', 'Windows NT 10.0; Win64; x64'),
            'cpu_cores': fingerprint.get('cpu_cores', 8),
            'memory': fingerprint.get('memory', 16),
            'webgl_vendor': fingerprint.get('webgl_vendor', 'Google Inc. (NVIDIA)'),
            'webgl_renderer': fingerprint.get('webgl_renderer', 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0'),
            'fonts': fingerprint.get('fonts', ['Segoe UI', 'Arial', 'Times New Roman']),
            'is_mobile': fingerprint.get('is_mobile', False),
            'has_touch': fingerprint.get('has_touch', False),
            'max_touch_points': fingerprint.get('max_touch_points', 0),
            'canvas_seed': fingerprint.get('canvas_seed', 12345),
            'audio_seed': fingerprint.get('audio_seed', 12345),
            'screen_width': fingerprint.get('screen_width', 1920),
            'screen_height': fingerprint.get('screen_height', 1080),
            'timezone_offset': fingerprint.get('timezone_offset', -300),
            'connection_type': fingerprint.get('connection_type', '4g'),
            'connection_downlink': fingerprint.get('connection_downlink', 10),
            'connection_rtt': fingerprint.get('connection_rtt', 50),
        }
        
        # First, try to apply playwright-stealth if available
        if PLAYWRIGHT_STEALTH_AVAILABLE:
            try:
                stealth_engine = Stealth(
                    navigator_languages_override=(fp['language'], 'en', 'es', 'fr', 'de'),
                    navigator_platform_override=fp['platform'],
                )
                await stealth_engine.apply_stealth_async(context)
                logger.debug(f"[STEALTH] Applied playwright-stealth evasions for session {session_id}")
            except Exception as e:
                logger.warning(f"[STEALTH] playwright-stealth failed: {e}, using custom evasions")
        
        # Apply custom advanced evasion scripts with fingerprint data
        await self._apply_advanced_evasions(context, session_id, fp)
        await self._apply_floating_label_lock(context, session_id)

        # Layer in the additional hardening patch set that closes gaps
        # the basic stealth scripts miss (window.outerWidth, Notification,
        # Error stack scrubbing, RTC ICE leak, version-consistent
        # userAgentData, etc.)
        await self._apply_stealth_hardening(
            context, session_id, fp, is_mobile=False
        )

    async def _apply_advanced_evasions(self, context, session_id: str, fingerprint: Dict = None):
        """
        Apply advanced custom evasion scripts that go beyond basic playwright-stealth.
        Uses fingerprint data for CONSISTENT spoofing values.
        CRITICAL: Values are generated ONCE per user and reused forever.
        """
        
        # Get fingerprint with defaults
        if fingerprint is None:
            fingerprint = {}
        
        fp = fingerprint
        
        # Generate seeded random from fingerprint for consistent canvas/audio noise
        # NOT random per request - this ensures same fingerprint always produces same noise
        seed = fp.get('canvas_seed', 12345)
        
        # Ultra-comprehensive stealth JavaScript with fingerprint values
        advanced_stealth_script = rf"""
        (function() {{
            'use strict';
            
            // FINGERPRINT VALUES - from persistent fingerprint (NOT random per session)
            const FP = {json.dumps(fp)};
            const CANVAS_SEED = {seed};
            
            // DOMAINS THAT NEED LEGITIMATE API ACCESS (2FA sites, canvas challenges, etc.)
            // Canvas noise and audio modifications DISABLED for these domains
            const SAFE_DOMAINS = [
                'yahoo.com', 'yahoo.co.uk', 'yahoo.com.au', 'yahoo.co.jp',
                'google.com', 'accounts.google.com',
                'facebook.com', 'login.facebook.com',
                'microsoft.com', 'login.microsoftonline.com',
                'apple.com', 'iforgot.apple.com',
                'amazon.com', 'signin.amazon.com',
                'twitter.com', 'x.com',
                'linkedin.com', 'login.linkedin.com',
                'github.com', 'gitlab.com', 'bitbucket.org',
                'paypal.com', 'signin.paypal.com',
                'dropbox.com', 'reddit.com',
                'instagram.com', 'tiktok.com',
                'snapchat.com', 'whatsapp.com',
                'wechat.com', 'line.me',
                'discord.com', 'slack.com',
                'zoom.us', 'teams.microsoft.com',
                'bankofamerica.com', 'chase.com', 'wellsfargo.com', 'citi.com',
            ];
            
            const currentHost = window.location.hostname.toLowerCase();
            const isSafeDomain = SAFE_DOMAINS.some(domain => currentHost.includes(domain));
            
            // Seeded random - CONSISTENT per user (NOT per request)
            let _seed = CANVAS_SEED;
            const seededRandom = () => {{
                _seed = (_seed * 16807) % 2147483647;
                return _seed / 2147483647;
            }};
            
            // ==================== ULTIMATE AUTOMATION ELIMINATION ====================
            
            // 1. Remove webdriver property completely
            Object.defineProperty(navigator, 'webdriver', {{
                get: () => undefined,
                set: () {{}},
                configurable: false,
                enumerable: true
            }});
            
            // 2. Remove from prototype
            try {{
                delete navigator.__proto__.webdriver;
            }} catch(e) {{}}
            
            // 3. Remove ALL CDP automation markers
            const automationMarkers = [
                'cdc_adoQpoasnfa76pfcZLmcfl_Array',
                'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
                'cdc_adoQpoasnfa76pfcZLmcfl_Symbol',
                '__webdriver_evaluate', '__selenium_evaluate',
                '__webdriver_script_function', '__webdriver_script_func', '__webdriver_script_fn',
                '__fxdriver_evaluate', '__driver_unwrapped', '__webdriver_unwrapped',
                '__driver_evaluate', '__selenium_unwrapped', '__fxdriver_unwrapped',
                '_Selenium_IDE_Recorder', '_selenium_evaluateLoadedCrowdDoc',
                '__driverFunc', '$chrome_asyncScriptInfo', '$cdc_asdjflasutopfhvcZLmcfl_', '__pdci',
            ];
            
            automationMarkers.forEach(marker => {{
                try {{
                    if (window[marker] !== undefined) {{
                        delete window[marker];
                    }}
                }} catch(e) {{}}
            }});
            
            // ==================== CHROME RUNTIME SPOOFING ====================
            try {{
                if (window.chrome) {{
                    window.chrome.runtime = {{
                        connect: () => ({{}}),
                        connectNative: () => ({{}}),
                        getManifest: () => ({{ manifest_version: 3, name: 'Chrome', version: '147.0.0.0' }}),
                        getURL: (path) => `chrome-extension://jbloopgfdlanhjfgemomcfcplmkdgbobe/${{path}}`,
                        id: 'jbloopgfdlanhjfgemomcfcplmkdgbobe',
                        onConnect: {{ addListener: () => {{}} }},
                        onMessage: {{ addListener: () => {{}} }},
                        sendMessage: () => {{}},
                        sendNativeMessage: () => {{}},
                    }};
                    
                    window.chrome.app = {{
                        InstallState: {{ RUNNING: 'ready', INSTALLED: 'ready', DISABLED: 'disabled' }},
                        running: () => true,
                        getDetails: () => ({{}}),
                        isInstalled: () => true,
                        LaunchType: {{ PINNED: 'pinned', REGULAR: 'regular', SHORTCUT: 'shortcut' }},
                    }};
                    
                    window.chrome.csi = () => ({{
                        onDomContentLoaded: Date.now(), onLoad: Date.now(),
                        pageAction: {{}}, navigation: {{}}, responseEnd: Date.now(),
                        startInteractive: Date.now(), startLoad: Date.now(),
                    }});
                    
                    window.chrome.loadTimes = () => ({{
                        commitLoadTime: Date.now() - 1000, connectionInfo: 'http/1.1',
                        documentLoadTime: 500, domContentLoadedEventEnd: Date.now() - 500,
                        domContentLoadedEventStart: Date.now() - 600, domInteractive: Date.now() - 700,
                        finishLoadTime: Date.now(), firstPaintAfterLoadTime: 0,
                        firstPaintTime: Date.now() - 800, navigationType: 'Other',
                        nrNavigationId: null, onCommitTransferSize: 0,
                        onDidCommitNavigation: {{}}, onDidFinishNavigation: {{}},
                        onDOMContentLoaded: {{}}, onFirstPaint: {{}}, onLoad: {{}},
                        onRCdpInfo: {{}}, originalRequestAddressIsReplaceable: false,
                        originalRequestMethodIsReplaceable: false, protocol: 'h2',
                        readyState: 'complete', receiveHeadersEnd: Date.now() - 100,
                        requestTime: Date.now() - 1100, sendEnd: Date.now() - 200,
                        startTime: Date.now() - 1200, transferSize: 0,
                        workingThroughBackForwardCache: false,
                    }});
                }}
            }} catch(e) {{}}
            
            // ==================== NAVIGATOR SPOOFING (from fingerprint) ====================
            
            // Languages - from fingerprint (matches proxy location)
            const languages = FP.languages || ['{fp.get('language', 'en-US')}', 'en'];
            Object.defineProperty(navigator, 'languages', {{
                get: () => languages,
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'language', {{
                get: () => FP.language || 'en-US',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'locale', {{
                get: () => FP.language || 'en-US',
                configurable: false,
                enumerable: true
            }});
            
            // Hardware concurrency - from fingerprint (consistent per user)
            Object.defineProperty(navigator, 'hardwareConcurrency', {{
                get: () => FP.cpu_cores || 8,
                configurable: false,
                enumerable: true
            }});
            
            // Device memory - from fingerprint (consistent per user)
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => FP.memory || 16,
                configurable: false,
                enumerable: true
            }});
            
            // Platform - from fingerprint (matches User Agent)
            Object.defineProperty(navigator, 'platform', {{
                get: () => FP.platform || 'Win32',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'oscpu', {{
                get: () => FP.oscpu || 'Windows NT 10.0; Win64; x64',
                configurable: false,
                enumerable: true
            }});
            
            // App version spoofing
            Object.defineProperty(navigator, 'appVersion', {{
                get: () => navigator.userAgent || '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'appCodeName', {{
                get: () => 'Mozilla',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'appName', {{
                get: () => 'Netscape',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'vendor', {{
                get: () => 'Google Inc.',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'product', {{
                get: () => 'Gecko',
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'productSub', {{
                get: () => '20030107',
                configurable: false,
                enumerable: true
            }});
            
            // ==================== USER AGENT DATA SPOOFING ====================
            // CRITICAL: brand/version must match the actual UA string.
            // Hardcoding version '120' here while the UA says 'Chrome/147'
            // is a one-click fingerprint mismatch that fingerprinted
            // libraries (FingerprintJS Pro, Cloudflare Bot Management,
            // PerimeterX) use as a definitive automation signal.
            if (navigator.userAgentData) {{
                const isMobile = FP.is_mobile || false;
                const ua = navigator.userAgent || '';
                const m = ua.match(/Chrome\/(\d+)/);
                const major = m ? m[1] : '147';
                const fullVer = major + '.0.0.0';
                const fpPlatform = (FP.platform || '').toLowerCase();
                let uaPlatform = 'Windows';
                let uaPlatformVersion = '15.0.0';
                let uaArch = 'x86';
                if (fpPlatform.indexOf('win') !== -1) {{
                    uaPlatform = 'Windows NT 10.0';
                    uaPlatformVersion = '15.0.0';
                    uaArch = 'x86';
                }} else if (fpPlatform.indexOf('mac') !== -1 || fpPlatform.indexOf('intel') !== -1) {{
                    uaPlatform = 'macOS';
                    uaPlatformVersion = '14.5.1';
                    uaArch = 'arm';
                }} else if (fpPlatform.indexOf('linux') !== -1 || fpPlatform.indexOf('android') !== -1) {{
                    if (isMobile) {{ uaPlatform = 'Android'; uaPlatformVersion = '15.0.0'; uaArch = 'arm'; }}
                    else {{ uaPlatform = 'Linux'; uaPlatformVersion = '6.5.0'; uaArch = 'x86'; }}
                }}
                const _uaList = [
                    {{ brand: 'Not_A Brand', version: '24.0.0.0' }},
                    {{ brand: 'Google Chrome', version: fullVer }},
                    {{ brand: 'Chromium', version: fullVer }}
                ];
                Object.defineProperty(navigator, 'userAgentData', {{
                    get: () => ({{
                        brands: _uaList.slice(0, 2),
                        mobile: isMobile,
                        platform: uaPlatform,
                        getHighEntropyValues: (hints) => Promise.resolve({{
                            architecture: uaArch,
                            bitness: '64',
                            brands: _uaList,
                            mobile: isMobile,
                            model: '',
                            platform: uaPlatform,
                            platformVersion: uaPlatformVersion,
                            uaFullVersion: fullVer,
                            fullVersionList: _uaList,
                            wow64: false
                        }}),
                        toJSON: function() {{ return {{ brands: this.brands, mobile: this.mobile, platform: this.platform }}; }}
                    }}),
                    configurable: false,
                    enumerable: true
                }});
            }}
            
            // ==================== PLUGINS SPOOFING ====================
            const mimeTypes = [
                {{ type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf' }},
                {{ type: 'text/pdf', description: 'Portable Document Format', suffixes: 'pdf' }},
            ];
            
            const plugins = [
                {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 }},
                {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format', length: 0 }},
                {{ name: 'Native Client', filename: 'internal-nacl-plugin', description: 'Native Client', length: 0 }},
            ];
            
            Object.defineProperty(navigator, 'plugins', {{
                get: () => plugins,
                configurable: false,
                enumerable: true
            }});
            
            Object.defineProperty(navigator, 'mimeTypes', {{
                get: () => mimeTypes,
                configurable: false,
                enumerable: true
            }});
            
            // ==================== WEBGL FINGERPRINT SPOOFING (from fingerprint) ====================
            const SPOOFED_VENDOR = FP.webgl_vendor || 'Google Inc. (NVIDIA)';
            const SPOOFED_RENDERER = FP.webgl_renderer || 'NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0';
            
            const origGetContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, attributes) {{
                const context = origGetContext.call(this, type, attributes);
                
                if (context && (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl')) {{
                    if (!context._stealthPatched) {{
                        context._stealthPatched = true;
                        
                        const origGetParam = context.getParameter.bind(context);
                        context.getParameter = function(param) {{
                            if (param === context.VENDOR) return SPOOFED_VENDOR;
                            if (param === context.UNMASKED_VENDOR_WEBGL) return SPOOFED_VENDOR;
                            if (param === context.RENDERER) return SPOOFED_RENDERER;
                            if (param === context.UNMASKED_RENDERER_WEBGL) return SPOOFED_RENDERER;
                            return origGetParam(param);
                        }};
                        
                        const origGetExt = context.getExtension.bind(context);
                        context.getExtension = function(ext) {{
                            const extension = origGetExt(ext);
                            if (ext === 'WEBGL_debug_renderer_info' && extension) {{
                                const origGetParamExt = extension.getParameter.bind(extension);
                                extension.getParameter = function(param) {{
                                    if (param === extension.UNMASKED_VENDOR_WEBGL) return SPOOFED_VENDOR;
                                    if (param === extension.UNMASKED_RENDERER_WEBGL) return SPOOFED_RENDERER;
                                    return origGetParamExt(param);
                                }};
                            }}
                            return extension;
                        }};
                    }}
                }}
                return context;
            }};
            
            // ==================== CANVAS FINGERPRINT NOISE (consistent per user) ====================
            // DISABLED for safe domains (2FA sites, canvas challenges)
            const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            CanvasRenderingContext2D.prototype.getImageData = function(sx, sy, sw, sh) {{
                const imageData = origGetImageData.call(this, sx, sy, sw, sh);
                
                // Only add noise if NOT on a safe domain (2FA sites need clean canvas)
                if (!isSafeDomain && imageData && imageData.data && seededRandom() > 0.5) {{
                    const data = imageData.data;
                    const noiseCount = Math.floor(data.length / 100);
                    
                    for (let i = 0; i < noiseCount; i++) {{
                        const idx = Math.floor(seededRandom() * data.length / 4) * 4;
                        const noise = Math.floor(seededRandom() * 3) - 1;
                        
                        if (idx + 0 < data.length) data[idx + 0] = Math.max(0, Math.min(255, data[idx + 0] + noise));
                        if (idx + 1 < data.length) data[idx + 1] = Math.max(0, Math.min(255, data[idx + 1] + noise));
                        if (idx + 2 < data.length) data[idx + 2] = Math.max(0, Math.min(255, data[idx + 2] + noise));
                    }}
                }}
                return imageData;
            }};
            
            // ==================== AUDIO CONTEXT FINGERPRINT SPOOFING (consistent per user) ====================
            // DISABLED for safe domains (2FA sites that use audio fingerprinting)
            const origAudioContext = window.AudioContext || window.webkitAudioContext;
            if (origAudioContext && !isSafeDomain) {{
                window.AudioContext = function(options) {{
                    const ctx = new origAudioContext(options);
                    
                    const origGetOutputTimestamp = ctx.getOutputTimestamp.bind(ctx);
                    ctx.getOutputTimestamp = () => {{
                        const ts = origGetOutputTimestamp();
                        return {{
                            contextTime: ts.contextTime + (seededRandom() * 0.0001),
                            performanceTime: ts.performanceTime,
                        }};
                    }};
                    
                    const origCreateOsc = ctx.createOscillator.bind(ctx);
                    ctx.createOscillator = function() {{
                        const osc = origCreateOsc();
                        const origStart = osc.start.bind(osc);
                        osc.start = function(time) {{
                            return origStart(time + seededRandom() * 0.0001);
                        }};
                        return osc;
                    }};
                    return ctx;
                }};
                window.OfflineAudioContext = window.OfflineAudioContext || origAudioContext;
            }}
            
            // ==================== TIMEZONE SPOOFING (from fingerprint - matches proxy location) ====================
            const TIMEZONE_OFFSET = FP.timezone_offset || -300;
            const TIMEZONE = FP.timezone || 'America/New_York';
            
            Object.defineProperty(Date.prototype, 'getTimezoneOffset', {{
                get: () => TIMEZONE_OFFSET,
                configurable: true,
            }});
            
            const origDateTimeFormat = Intl.DateTimeFormat;
            Intl.DateTimeFormat = function(locale, options) {{
                const formatter = new origDateTimeFormat(locale, options);
                const origResolved = formatter.resolvedOptions.bind(formatter);
                formatter.resolvedOptions = function() {{
                    const options = origResolved();
                    options.timeZone = TIMEZONE;
                    options.timeZoneName = 'short';
                    return options;
                }};
                return formatter;
            }};
            Intl.DateTimeFormat.prototype = origDateTimeFormat.prototype;
            
            // ==================== PERMISSIONS API SPOOFING ====================
            if (navigator.permissions) {{
                const origQuery = navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query = function(options) {{
                    if (options.name === 'notifications') {{
                        return Promise.resolve({{ state: 'granted', onchange: null }});
                    }}
                    if (options.name === 'geolocation') {{
                        return Promise.resolve({{ state: 'prompt', onchange: null }});
                    }}
                    return origQuery(options);
                }};
            }}
            
            // ==================== CONNECTION API SPOOFING (from fingerprint) ====================
            if (navigator.connection) {{
                Object.defineProperty(navigator, 'connection', {{
                    get: () => ({{
                        effectiveType: FP.connection_type || '4g',
                        downlink: FP.connection_downlink || 10,
                        downlinkMax: 20,
                        rtt: FP.connection_rtt || 50,
                        saveData: false,
                        onchange: null,
                        addEventListener: () => {{}},
                        removeEventListener: () => {{}},
                    }}),
                    configurable: false,
                    enumerable: true
                }});
            }}
            
            // ==================== BATTERY API SPOOFING ====================
            if (navigator.getBattery) {{
                navigator.getBattery = () => Promise.resolve({{
                    charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1,
                    onchargingchange: null, onchargingtimechange: null,
                    ondischargingtimechange: null, onlevelchange: null,
                }});
            }}
            
            // ==================== MEDIA DEVICES SPOOFING ====================
            // For safe domains (2FA sites), return more complete fake devices
            // For other sites, return minimal devices to prevent fingerprinting
            if (navigator.mediaDevices) {{
                const originalEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
                navigator.mediaDevices.enumerateDevices = () => {{
                    if (isSafeDomain) {{
                        // 2FA sites get more complete fake devices
                        return Promise.resolve([
                            {{ deviceId: 'default', kind: 'audioinput', label: 'Built-in Microphone', groupId: 'group_0' }},
                            {{ deviceId: 'communications', kind: 'audioinput', label: 'Communication Microphone', groupId: 'group_1' }},
                            {{ deviceId: 'videoinput', kind: 'videoinput', label: 'Built-in Camera', groupId: 'group_2' }},
                            {{ deviceId: 'audiooutput', kind: 'audiooutput', label: 'Built-in Speakers', groupId: 'group_3' }},
                        ]);
                    }} else {{
                        // Other sites get minimal devices
                        return Promise.resolve([
                            {{ deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' }},
                            {{ deviceId: 'communications', kind: 'audioinput', label: '', groupId: 'communications' }},
                        ]);
                    }}
                }};
            }}
            
            // ==================== TOUCH SUPPORT (from fingerprint) ====================
            const MAX_TOUCH_POINTS = FP.max_touch_points || 0;
            Object.defineProperty(navigator, 'maxTouchPoints', {{
                get: () => MAX_TOUCH_POINTS,
                configurable: false,
                enumerable: true
            }});
            
            // ==================== SPEECH SYNTHESIS SPOOFING ====================
            if (window.speechSynthesis) {{
                const origGetVoices = window.speechSynthesis.getVoices.bind(window.speechSynthesis);
                window.speechSynthesis.getVoices = function() {{
                    const voices = origGetVoices();
                    if (voices.length === 0) {{
                        return [
                            {{ name: 'Google US English', lang: 'en-US', default: true }},
                            {{ name: 'Microsoft David Desktop', lang: 'en-US' }},
                        ];
                    }}
                    return voices;
                }};
            }}
            
            // ==================== SCREEN PROPERTIES SPOOFING ====================
            Object.defineProperty(screen, 'availWidth', {{
                get: () => screen.width,
                configurable: false,
            }});
            
            Object.defineProperty(screen, 'availHeight', {{
                get: () => screen.height - 40,
                configurable: false,
            }});
            
            Object.defineProperty(screen, 'availLeft', {{
                get: () => 0,
                configurable: false,
            }});
            
            Object.defineProperty(screen, 'availTop', {{
                get: () => 0,
                configurable: false,
            }});
            
            // ==================== VISUAL VIEWPORT SPOOFING ====================
            if (window.visualViewport) {{
                Object.defineProperty(window.visualViewport, 'width', {{
                    get: () => window.innerWidth,
                    configurable: false,
                }});
                
                Object.defineProperty(window.visualViewport, 'height', {{
                    get: () => window.innerHeight,
                    configurable: false,
                }});
            }}
            
            // ==================== CONSOLE ARTIFACT CLEANUP ====================
            const originalConsoleDebug = console.debug;
            console.debug = function(...args) {{
                if (args.some(arg => String(arg).includes('cdcd') || String(arg).includes('webdriver'))) {{
                    return;
                }}
                return originalConsoleDebug.apply(console, args);
            }};
            
            console.log('[STEALTH] Fingerprint-based evasion patches applied successfully');
        }})();
        """
        
        await context.add_init_script(advanced_stealth_script)
        logger.debug(f"[STEALTH] Applied fingerprint-based advanced evasions for session {session_id}")
        
        # Apply additional CDP-based stealth patches on existing pages
        try:
            for page in context.pages:
                try:
                    await page.evaluate('''() => {
                        // Remove any remaining automation indicators that might have been added
                        try {
                            const toRemove = [];
                            for (const key in window) {
                                if (key.includes('cdc_') || key.includes('__selenium') || 
                                    key.includes('__webdriver') || key.includes('__fxdriver')) {
                                    toRemove.push(key);
                                }
                            }
                            toRemove.forEach(key => {
                                try { delete window[key]; } catch(e) {}
                            });
                        } catch(e) {}
                    }''')
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[STEALTH] Error applying page-level evasions: {e}")
        # Balanced stealth for desktop - removes automation markers + basic spoofing, but no WebGL/canvas/audio/geo that breaks functionality
        stealth_script = r"""
        (function() {
            'use strict';
            
            // Remove CDP/Playwright automation markers
            Object.keys(window).forEach(key => {
                if (key.includes('cdc_') || key.includes('__webdriver') || key.includes('__selenium')) {
                    try { delete window[key]; } catch(e) {}
                }
            });
            
            // Hide webdriver property
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true
            });
            
            // Plugin spoofing (real Chrome has these)
            Object.defineProperty(navigator, 'plugins', {
                get: () => [
                    { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 },
                    { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '', length: 0 },
                    { name: 'Native Client', filename: 'internal-nacl-plugin', description: '', length: 0 }
                ],
                configurable: true
            });
            
            // Language spoofing
            Object.defineProperty(navigator, 'language', {
                get: () => 'en-US',
                configurable: true
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
                configurable: true
            });
        })();
        """
        
        await context.add_init_script(stealth_script)
    
    async def _setup_dialog_handler(self, context, session_id: str):
        """
        Set up dialog event handlers for the browser context.
        This handles alert, confirm, prompt, and beforeunload dialogs.
        """
        try:
            # Get all pages in the context
            for page in context.pages:
                await self._add_dialog_handler_to_page(page, session_id)
            
            # Also handle future pages (when new tabs are opened)
            context.on("page", lambda page: asyncio.create_task(
                self._add_dialog_handler_to_page(page, session_id)
            ))
            
        except Exception as e:
            logger.error(f"Error setting up dialog handler: {e}")
    
    async def _add_dialog_handler_to_page(self, page, session_id: str):
        """Add dialog handler to a specific page"""
        try:
            # Listen for dialog events
            page.on("dialog", lambda dialog: asyncio.create_task(
                self.dialog_handler.handle_dialog(dialog, session_id)
            ))
            logger.debug(f"Dialog handler set up for session {session_id}")
        except Exception as e:
            logger.error(f"Error adding dialog handler to page: {e}")
    
    async def _kick_out_session_by_user_id(self, user_id: str):
        """Deprecated compatibility hook; parent identity never owns a browser.

        Runtime sessions are keyed by their explicit session id.  Closing every
        browser that happens to share a durable profile would violate session
        isolation, so replacement must go through SessionManager with an
        explicit runtime id/owner check.
        """
        logger.debug("Ignoring profile-wide browser kick for parent %s", user_id)
        return 0

    async def _force_close_session(self, sess_id: str):
        """
        Force close a session and all its associated processes.
        FIXED: Better process cleanup with multiple termination attempts.
        """
        try:
            browser_info = self.get_active_browser(sess_id)
            if not browser_info:
                return
            
            logger.debug(f"Force closing session {sess_id}")
            
            # 1. Close direct Chrome browser process (multiple termination attempts)
            chrome_process = browser_info.get('chrome_process')
            if chrome_process:
                await self._terminate_process_gracefully(chrome_process)
            
            # 2. Close CDP connection
            cdp_connection = browser_info.get('cdp_connection')
            if cdp_connection:
                try:
                    await cdp_connection.close()
                except Exception as e:
                    logger.warning(f"CDP close error for {sess_id}: {e}")
            
            # 3. Close Playwright context and browser
            context = browser_info.get('context')
            browser = browser_info.get('browser')
            
            if context:
                try:
                    # Save cookies before closing
                    cookies = await context.cookies()
                    if cookies:
                        profile_user_id = browser_info.get('user_id', sess_id)
                        await self.profile_manager.save_cookies(profile_user_id, cookies)
                except Exception as e:
                    logger.warning(f"Cookie save error for {sess_id}: {e}")
                
                try:
                    await context.close()
                except Exception as e:
                    logger.warning(f"Context close error for {sess_id}: {e}")
            
            if browser:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning(f"Browser close error for {sess_id}: {e}")
            
            # 4. Unregister from GPU manager
            gpu_id = browser_info.get('gpu_id')
            if gpu_id is not None:
                self.gpu_manager.unregister_session(sess_id, gpu_id)
            
            # 5. Remove from active browsers
            with self._active_browsers_lock:
                self.active_browsers.pop(sess_id, None)
            
            # 6. FIX: Clean up any zombie processes for this session
            await self._cleanup_zombie_processes(sess_id)
            
            logger.debug(f"Successfully kicked out session {sess_id}")
        except Exception as e:
            logger.error(f"Error kicking out session {sess_id}: {e}")
    
    async def _terminate_process_gracefully(self, process: subprocess.Popen, timeout: float = 3.0):
        """
        Terminate a process with multiple fallback strategies.
        FIXED: Added zombie detection and force kill.
        """
        if not process:
            return
        
        try:
            # First attempt: graceful terminate
            process.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.create_subprocess_exec('wait', str(process.pid)),
                    timeout=timeout
                )
                return
            except asyncio.TimeoutExpired:
                pass
        except Exception as e:
            logger.warning(f"First terminate attempt failed: {e}")
        
        try:
            # Second attempt: SIGTERM via shell
            import platform as platform_module
            is_windows = platform_module.system() == 'Windows'
            
            if is_windows:
                await asyncio.create_subprocess_exec(
                    'taskkill', '/TERM', '/PID', str(process.pid),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            else:
                await asyncio.create_subprocess_shell(
                    f'kill -TERM {process.pid}',
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )
            await asyncio.sleep(0.5)
            
            # Check if still running
            if process.poll() is None:
                raise Exception("Process still running after SIGTERM")
            return
        except Exception as e:
            logger.warning(f"Second terminate attempt failed: {e}")
        
        try:
            # Third attempt: force kill with SIGKILL
            logger.warning(f"Force killing process {process.pid}")
            process.kill()
            try:
                await asyncio.wait_for(
                    asyncio.create_subprocess_exec('wait', str(process.pid)),
                    timeout=2.0
                )
            except asyncio.TimeoutExpired:
                # Give it one more chance
                process.kill()
        except Exception as e:
            logger.error(f"Process kill failed: {e}")
    
    async def _cleanup_zombie_processes(self, session_id: str):
        """Reap only Chrome processes with this session's exact runtime profile."""
        try:
            info = self.get_active_browser(session_id)
            profile_dir = (info or {}).get('profile_dir')
            if not profile_dir or not sys.platform.startswith('linux'):
                return
            target = Path(profile_dir).resolve()
            root = (Path(self.config.profile_base_path) / '.runtime_sessions').resolve()
            if root not in target.parents:
                return
            for entry in os.listdir('/proc'):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                try:
                    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                    args = [part.decode(errors='replace') for part in raw.split(b'\0') if part]
                    actual = None
                    for index, arg in enumerate(args):
                        if arg == '--user-data-dir' and index + 1 < len(args):
                            actual = args[index + 1]
                            break
                        if arg.startswith('--user-data-dir='):
                            actual = arg.split('=', 1)[1]
                            break
                    if actual and Path(actual).resolve() == target:
                        os.kill(pid, 9)
                        logger.warning(
                            "Killed owned zombie Chrome process %s for session %s",
                            pid, session_id,
                        )
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass
                except Exception:
                    logger.debug("[Zombie] exact profile check failed for %s", pid, exc_info=True)
        except Exception:
            logger.debug("[Zombie] scoped cleanup failed for %s", session_id, exc_info=True)

    async def cleanup_all_zombies(self):
        """Run the GPU manager's private-runtime-only cleanup.

        This compatibility method intentionally does not scan or kill all
        Chrome processes on the host.
        """
        try:
            return await asyncio.to_thread(
                self.gpu_manager.cleanup_orphaned_chrome_processes
            )
        except Exception:
            logger.debug("[Zombie] scoped global cleanup failed", exc_info=True)
            return 0

    async def close_browser(self, browser, context, session_id: str, gpu_id: int):
        """Close browser and cleanup"""
        try:
            # Save cookies before closing
            if context:
                try:
                    user_id = getattr(context, 'user_id', session_id)
                    cookies = await context.cookies()
                    if cookies:
                        await self.profile_manager.save_cookies(user_id, cookies)
                except Exception:
                    pass
            
            if context:
                await context.close()
            if browser:
                await browser.close()
            self.gpu_manager.unregister_session(session_id, gpu_id)
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    async def solve_captcha(self, context) -> bool:
        """
        Solve CAPTCHA using SeleniumBase's built-in solver
        Returns True if CAPTCHA was solved successfully
        Note: This requires SeleniumBase to be installed and a valid CAPTCHA service
        """
        try:
            # Get profile directory from context
            profile_dir = getattr(context, 'profile_path', None)
            if not profile_dir:
                return False
            
            # SAFETY GUARD: never launch a second Chrome against a profile
            # that one of our sessions is already using - two Chrome
            # processes on one user-data-dir corrupt the profile lock and
            # can destroy the user's login state. Forward the CAPTCHA to
            # the user via the client (dialog/WS channel) instead.
            try:
                active_dir = os.path.abspath(profile_dir)
                for info in self.snapshot_active_browsers().values():
                    active_profile = info.get('profile_dir')
                    if active_profile and os.path.abspath(active_profile) == active_dir:
                        logger.warning(
                            "[CAPTCHA] A browser session is already running on this profile - "
                            "skipping SeleniumBase solver (a second Chrome on the same "
                            "profile would corrupt it). Forward the CAPTCHA to the user "
                            "via the client instead.")
                        return False
            except Exception:
                pass
            
            from seleniumbase import Driver
            
            # Create temporary SB driver for CAPTCHA solving
            # This doesn't affect the main Playwright browser
            sb_driver = Driver(
                browser="chrome",
                headless=True,
                user_data_dir=normalize_path_for_playwright(profile_dir),
                uc=True,
            )
            
            # Use SB's CAPTCHA solver
            result = False
            if hasattr(sb_driver, 'solve_captcha'):
                result = sb_driver.solve_captcha()
            
            sb_driver.quit()
            return result
        
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
        
        return False
    
    async def save_user_profile_info(self, user_id: str, user_agent: str, 
                                      viewport: Dict, current_url: str = None,
                                      country: str = None, state: str = None):
        """Save user profile information to About.txt"""
        browser_info = {
            'ip': self._get_client_ip(),
            'user_agent': user_agent,
            'browser': 'Chrome',
            'browser_version': '147.0.0.0',
            'platform': 'Windows NT 10.0; Win64; x64',
            'viewport': f"{viewport.get('width', 1920)}x{viewport.get('height', 1080)}",
            'pixel_ratio': str(viewport.get('pixelRatio', 1.0)),
            'current_url': current_url or 'None',
            'session_start': time.strftime('%Y-%m-%d %H:%M:%S'),
            'country': country or 'Unknown',
            'state': state or 'Unknown',
        }
        
        await self.profile_manager.save_user_info(user_id, browser_info)
    
    async def update_session_cookies(self, user_id: str, url: str, cookies: List[Dict]):
        """Update session cookies for a specific URL"""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).netloc
            await self.profile_manager.update_cookies(user_id, domain, cookies)
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    async def _apply_mobile_stealth(self, context, session_id: str, fingerprint: Dict = None):
        """
        Apply LIGHTWEIGHT MOBILE STEALTH - hide automation markers and common detection points.
        Uses fingerprint data for consistent spoofing.
        
        Balances between avoiding detection while not breaking site functionality.
        """
        
        # Use fingerprint values or fallback to defaults
        if fingerprint is None:
            fingerprint = {}
        
        platform = fingerprint.get('platform', 'iPhone')
        cpu_cores = fingerprint.get('cpu_cores', 8)
        memory = fingerprint.get('memory', 4)
        webgl_vendor = fingerprint.get('webgl_vendor', 'Apple Inc.')
        webgl_renderer = fingerprint.get('webgl_renderer', 'Apple GPU')
        fonts = fingerprint.get('fonts', ['SF Pro Display', 'SF Pro Text', 'Helvetica Neue', 'Arial', 'Times New Roman'])
        language = fingerprint.get('language', 'en-US')
        timezone = fingerprint.get('timezone', 'America/New_York')
        max_touch_points = fingerprint.get('max_touch_points', 5)
        is_mobile = fingerprint.get('is_mobile', True)
        # Extract Chrome version + model name from the user agent so the
        # Sec-CH-UA / userAgentData spoof matches the UA string.
        raw_ua = fingerprint.get('user_agent', '') or ''
        ua_chrome_version = '147'
        ua_match = __import__('re').search(r'Chrome/(\d+)', raw_ua)
        if ua_match:
            ua_chrome_version = ua_match.group(1)
        ua_model = ''
        ua_model_match = __import__('re').search(r'\((?:iPhone|Linux; Android [^;]+; )([^)]+)\)', raw_ua)
        if ua_model_match:
            ua_model = ua_model_match.group(1).strip()
        if 'iPhone' in raw_ua:
            ua_model = ''  # real iOS getHighEntropyValues reports no model
        # userAgentData platform: real Chrome on Android reports "Android";
        # iOS Safari/WebKit reports "iOS". Map our fingerprint.platform to those.
        ua_data_platform = 'iOS' if 'iPhone' in platform or 'iOS' in platform else 'Android'
        # platformVersion: derived from the UA (real iOS reports 3-part like
        # "18.3.0", real Android 4-part like "15.0.0.0") - never hardcoded.
        import re as _re_stealth
        ua_platform_version = '15.0.0.0'
        _ios_pv = _re_stealth.search(r'iphone os (\d+)_(\d+)', raw_ua.lower())
        _and_pv = _re_stealth.search(r'android (\d+)', raw_ua.lower())
        if ua_data_platform == 'iOS' and _ios_pv:
            ua_platform_version = f'{_ios_pv.group(1)}.{_ios_pv.group(2)}.0'
        elif _and_pv:
            ua_platform_version = f'{_and_pv.group(1)}.0.0.0'
        # iOS Safari has NO plugins, NO mimeTypes and NO Battery API - their
        # presence on an "iPhone" is an instant bot tell.
        is_ios = ua_data_platform == 'iOS'
        
        # ENHANCED mobile stealth with aggressive bot detection evasion
        light_mobile_stealth_script = rf"""
        (function() {{
            'use strict';

            // 0. userAgentData (Sec-CH-UA) - CRITICAL for modern bot detection.
            // Real Chrome on Android/iOS exposes navigator.userAgentData with brand
            // + version + mobile + platform + getHighEntropyValues(). Many sites
            // (Cloudflare, PerimeterX, etc.) read this via Sec-CH-UA headers and
            // JS APIs to classify the browser.
            try {{
                const __uaDataPlatform = '{ua_data_platform}';
                const __uaChromeVer = '{ua_chrome_version}';
                const __isMobile = {str(is_mobile).lower()};
                const __isIos = {str(is_ios).lower()};
                const __uaModel = '{ua_model}';
                const __uaFullList = [
                    {{ brand: 'Chromium', version: __uaChromeVer }},
                    {{ brand: 'Google Chrome', version: __uaChromeVer }},
                    {{ brand: 'Not_A Brand', version: '24' }}
                ];
                const __uaData = {{
                    brands: __uaFullList.slice(0, 2),
                    mobile: __isMobile,
                    platform: __uaDataPlatform,
                    getHighEntropyValues: function(hints) {{
                        const out = {{
                            architecture: __uaDataPlatform === 'iOS' ? '' : 'arm',
                            bitness: '64',
                            brands: __uaFullList,
                            mobile: __isMobile,
                            model: __uaModel || '',
                            platform: __uaDataPlatform,
                            platformVersion: '{ua_platform_version}',
                            uaFullVersion: __uaChromeVer + '.0.0.0',
                            fullVersionList: __uaFullList,
                            wow64: false
                        }};
                        return Promise.resolve(out);
                    }},
                    toJSON: function() {{ return {{ brands: this.brands, mobile: this.mobile, platform: this.platform }}; }}
                }};
                Object.defineProperty(Navigator.prototype, 'userAgentData', {{
                    get: function() {{ return __uaData; }},
                    configurable: true,
                    enumerable: true
                }});
            }} catch(e) {{}}

            // 1. Remove navigator.webdriver completely
            Object.defineProperty(navigator, 'webdriver', {{
                get: () => undefined,
                configurable: true,
                enumerable: false
            }});
            
            // 2. Remove ALL CDP/Playwright/Selenium automation markers
            Object.keys(window).forEach(key => {{
                if (key.includes('cdc_') || key.includes('__webdriver') || 
                    key.includes('__selenium') || key.includes('__fxdriver') ||
                    key.includes('__driver_') || key.includes('selenium') ||
                    key.includes('$chrome_asyncScriptInfo') || key.includes('ChromeDriver') ||
                    key.includes('WEBDRIVER') || key.includes('_selenium') ||
                    key.includes('_WEBDRIVER_ELEM_CACHE') || key.includes('_COS_') ||
                    key.includes('puppeteer') || key.includes('nightmarejs') ||
                    key.includes('phantom') || key.includes('headless')) {{
                    try {{ delete window[key]; }} catch(e) {{}}
                }}
            }});
            
            // 3. Enhanced Chrome object with realistic APIs
            if (!window.chrome) {{
                Object.defineProperty(window, 'chrome', {{
                    get: () => ({{
                        runtime: {{ 
                            connect: () => {{}}, 
                            sendMessage: () => {{}},
                            onConnect: {{ addListener: () => {{}} }},
                            onMessage: {{ addListener: () => {{}} }},
                            onInstalled: {{ addListener: () => {{}} }},
                            getURL: (path) => 'chrome-extension://noextension/' + path
                        }},
                        loadTimes: () => ({{
                            commitLoadTime: Date.now() - Math.random() * 5000,
                            connectionInfo: 'h2',
                            documentLoadTime: Date.now() - Math.random() * 3000,
                            domContentLoadedEventEnd: Date.now() - Math.random() * 2000,
                            domContentLoadedEventStart: Date.now() - Math.random() * 3000,
                            domInteractive: Date.now() - Math.random() * 2500,
                            finishLoadTime: Date.now(),
                            firstPaintAfterLoadTime: Date.now() - Math.random() * 1000,
                            firstPaintTime: Date.now() - Math.random() * 2000,
                            navigationType: 'Other',
                            protocol: 'h2',
                            readyState: 'complete',
                            startTime: Date.now() - Math.random() * 8000,
                            transferSize: Math.floor(Math.random() * 500000)
                        }}),
                        csi: () => ({{
                            onDomContentLoaded: Date.now() - Math.random() * 3000,
                            onLoad: Date.now() - Math.random() * 2000,
                            pageAction: {{}},
                            navigation: {{}},
                            responseEnd: Date.now() - Math.random() * 5000
                        }}),
                        app: {{ 
                            getDetails: () => ({{}}), 
                            isInstalled: () => false,
                            InstallState: {{ RUNNING: 'ready', INSTALLED: 'installed' }},
                            running: () => true
                        }},
                        scripting: {{
                            executeScript: () => {{}},
                            insertCSS: () => {{}}
                        }},
                        tabs: {{
                            query: () => Promise.resolve([]),
                            create: () => {{}},
                            update: () => {{}}
                        }}
                    }}),
                    configurable: true,
                    enumerable: false
                }});
            }}
            
            // 4. Real plugin/mime-type spoofing - ANDROID ONLY. iOS Safari
            // has zero plugins/mimeTypes; spoofing them on an iPhone is an
            // instant bot tell.
            if (!__isIos) {{
            Object.defineProperty(navigator, 'plugins', {{
                get: () => ({{
                    0: {{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 }},
                    1: {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format', length: 0 }},
                    2: {{ name: 'Native Client', filename: 'internal-nacl-plugin', description: 'Native Client', length: 0 }},
                    length: 3,
                    item: (i) => [{{ name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1 }}, {{ name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format', length: 0 }}, {{ name: 'Native Client', filename: 'internal-nacl-plugin', description: 'Native Client', length: 0 }}][i],
                    namedItem: (n) => null,
                    refresh: () => {{}}
                }}),
                configurable: true,
                enumerable: false
            }});
            
            Object.defineProperty(navigator, 'mimeTypes', {{
                get: () => ({{
                    0: {{ type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf', enabledPlugin: {{ name: 'Chrome PDF Plugin' }} }},
                    1: {{ type: 'application/x-nacl', description: 'Native Client', suffixes: '', enabledPlugin: {{ name: 'Native Client' }} }},
                    length: 2,
                    item: (i) => [{{ type: 'application/pdf', description: 'Portable Document Format', suffixes: 'pdf', enabledPlugin: {{ name: 'Chrome PDF Plugin' }} }}, {{ type: 'application/x-nacl', description: 'Native Client', suffixes: '', enabledPlugin: {{ name: 'Native Client' }} }}][i],
                    namedItem: (n) => null
                }}),
                configurable: true,
                enumerable: false
            }});
            }} // end !__isIos (plugins/mimeTypes - Android only)
                
            // 5. Language spoofing
            Object.defineProperty(navigator, 'language', {{
                get: () => '{language}',
                configurable: true
            }});
            Object.defineProperty(navigator, 'languages', {{
                get: () => ['{language}', 'en', 'es', 'fr', 'de'],
                configurable: true
            }});
            
            // 6. Platform spoofing for mobile
            Object.defineProperty(navigator, 'platform', {{
                get: () => '{platform}',
                configurable: true
            }});
            
            // 7. User Agent (force it from fingerprint so the JS-side UA matches
            // the HTTP User-Agent header and the userAgentData brands/version).
            try {{
                Object.defineProperty(Navigator.prototype, 'userAgent', {{
                    get: function() {{ return {json.dumps(raw_ua)}; }},
                    configurable: true,
                    enumerable: true
                }});
            }} catch(e) {{
                try {{
                    Object.defineProperty(navigator, 'userAgent', {{
                        get: () => {json.dumps(raw_ua)},
                        configurable: true
                    }});
                }} catch(e2) {{}}
            }}
            
            // 8. Permissions API - realistic responses
            if (navigator.permissions) {{
                const origQuery = navigator.permissions.query.bind(navigator.permissions);
                navigator.permissions.query = function(desc) {{
                    const name = desc && desc.name;
                    if (name === 'notifications') return Promise.resolve({{ state: 'default' }});
                    if (name === 'geolocation') return Promise.resolve({{ state: 'prompt' }});
                    if (name === 'camera') return Promise.resolve({{ state: 'denied' }});
                    if (name === 'microphone') return Promise.resolve({{ state: 'denied' }});
                    if (name === 'payment') return Promise.resolve({{ state: 'denied' }});
                    return Promise.resolve({{ state: 'denied' }});
                }};
            }}
            
            // 9. Hide webdriver from __proto__
            try {{
                Object.defineProperty(Object.getPrototypeOf(navigator), 'webdriver', {{
                    get: () => undefined,
                    configurable: true
                }});
            }} catch(e) {{}}
            
            // 10. Hardware concurrency
            Object.defineProperty(navigator, 'hardwareConcurrency', {{
                get: () => {cpu_cores},
                configurable: true
            }});
            
            // 11. Device memory
            Object.defineProperty(navigator, 'deviceMemory', {{
                get: () => {memory},
                configurable: true
            }});
            
            // 12. Touch support for mobile (use fingerprint value, not a hardcoded number)
            Object.defineProperty(navigator, 'maxTouchPoints', {{
                get: () => {max_touch_points},
                configurable: true
            }});
            
            // 13. Enhanced WebGL spoofing
            const origGetContext = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, attributes) {{
                const context = origGetContext.call(this, type, attributes);
                if (context && (type === 'webgl' || type === 'webgl2' || type === '2d')) {{
                    if (type === 'webgl' || type === 'webgl2') {{
                        const origGetParam = context.getParameter.bind(context);
                        context.getParameter = function(param) {{
                            if (param === 37445) return '{webgl_vendor}';
                            if (param === 37446) return '{webgl_renderer}';
                            if (param === 7938) return 'WebGL GLSL ES 1.0';
                            if (param === 35724) return 'WebGL Vendor';
                            if (param === 35725) return '{webgl_vendor}';
                            return origGetParam(param);
                        }};
                    }}
                }}
                return context;
            }};
            
            // 14. Media devices spoofing
            if (navigator.mediaDevices) {{
                const origEnumerate = navigator.mediaDevices.enumerateDevices.bind(navigator.mediaDevices);
                navigator.mediaDevices.enumerateDevices = () => Promise.resolve([
                    {{ deviceId: 'default', kind: 'audioinput', label: 'Default Audio Input', groupId: 'default' }},
                    {{ deviceId: 'communications', kind: 'audioinput', label: 'Communications Audio Input', groupId: 'communications' }},
                    {{ deviceId: 'default', kind: 'audiooutput', label: 'Default Audio Output', groupId: 'default' }},
                ]);
            }}
            
            // 15. Battery API spoofing - Android only (iOS Safari has no
            // Battery API; adding it to an iPhone is a bot tell)
            if (!__isIos && !navigator.getBattery && !navigator.battery) {{
                navigator.getBattery = () => Promise.resolve({{
                    level: 0.8 + Math.random() * 0.2,
                    charging: true,
                    chargingTime: Math.random() * 3600,
                    dischargingTime: Infinity,
                    onlevelchange: null,
                    onchargingchange: null,
                    onchargingtimechange: null,
                    ondischargingtimechange: null,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}}
                }});
            }}
            
            // 16. Network information spoofing  
            if (navigator.connection || navigator.mozConnection || navigator.webkitConnection) {{
                const fakeConnection = {{
                    downlink: 10,
                    effectiveType: '4g',
                    rtt: Math.random() * 50 + 20,
                    saveData: false,
                    onchange: null,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}}
                }};
                if (navigator.connection) Object.defineProperty(navigator, 'connection', {{ get: () => fakeConnection, configurable: true }});
                if (navigator.mozConnection) Object.defineProperty(navigator, 'mozConnection', {{ get: () => fakeConnection, configurable: true }});
                if (navigator.webkitConnection) Object.defineProperty(navigator, 'webkitConnection', {{ get: () => fakeConnection, configurable: true }});
            }}
            
            // 17. Prevent WebRTC IP leak
            const origRTCPeerConnection = RTCPeerConnection || webkitRTCPeerConnection || mozRTCPeerConnection;
            if (origRTCPeerConnection) {{
                RTCPeerConnection = function(...args) {{
                    const pc = new origRTCPeerConnection(...args);
                    pc.createDataChannel = new Proxy(pc.createDataChannel, {{
                        apply: (target, thisArg, argList) => target.apply(thisArg, argList)
                    }});
                    return pc;
                }};
            }}
            
            // 18. Hide any frame/sandbox indicators
            try {{
                Object.defineProperty(window, 'self', {{ get: () => window, configurable: false }});
            }} catch(e) {{}}
        }})();
        """
        
        await context.add_init_script(light_mobile_stealth_script)
        await self._apply_floating_label_lock(context, session_id)
        # Layer in the additional hardening extras (Notification permission,
        # outerWidth, Error stack scrubbing, RTC ICE leak, etc.) — same
        # kernel as desktop; pass mobile context so the few platform-
        # dependent bits (touch handler, mobile-screen sizes) line up.
        await self._apply_stealth_hardening(
            context, session_id, fingerprint, is_mobile=True
        )
        logger.debug(f"[Mobile Stealth] Applied fingerprint-based stealth for session {session_id}")

    async def _apply_floating_label_lock(self, context, session_id: str):
        """
        Permanently lock floating-label inputs in the "up" position once the user
        has typed their first character.

        Background:
            Many sites (and Chrome's own autofill UI) animate a field's
            placeholder/label upward when the input is focused, then animate it
            back down when blurred if the field is empty. This is jarring and
            can interfere with our interaction layer if the label sits on top
            of an interactive element after the user has already started
            typing.

        Behavior:
            On the FIRST `input` event for each input/textarea, this script:
              1. Marks the input with `data-floating-label-locked="1"`.
              2. Finds the associated floating label (parent <label>, sibling
                 <label>, or <label for="...">).
              3. Forces the label up via inline style + a CSS rule keyed on the
                 attribute.
              4. Attaches a MutationObserver that re-asserts the lock if any
                 framework code (MUI, Materialize, custom) tries to remove
                 the data attribute, the inline transform, or the inline
                 font-size.
              5. Also stubs the input's blur handler so frameworks can't
                 trigger their "animate label down" path.

        This is a targeted, field-scoped override. It does NOT blanket-disable
        animations site-wide (which we removed earlier because it broke
        loading spinners and gated content).
        """
        if context is None:
            return
        script = r"""
        (function() {
            'use strict';
            if (window.__floatingLabelLockInstalled) return;
            window.__floatingLabelLockInstalled = true;

            // 1. Inject permanent CSS that locks any element tagged
            //    [data-floating-label-locked] into the floated-up state.
            const style = document.createElement('style');
            style.id = '__floating_label_lock_css__';
            style.textContent = `
                [data-floating-label-locked="1"] {
                    transform: translateY(-100%) !important;
                    -webkit-transform: translateY(-100%) !important;
                    font-size: 0.75em !important;
                    top: 0 !important;
                    opacity: 1 !important;
                    pointer-events: none !important;
                }
            `;
            (document.head || document.documentElement).appendChild(style);

            // 2. Locate the floating label associated with an input.
            //    Tries the most common patterns: <label> wrapper (Material),
            //    sibling <label>, then explicit <label for="...">.
            function findFloatingLabel(input) {
                if (!input) return null;
                try {
                    const parentLabel = input.closest('label');
                    if (parentLabel) return parentLabel;
                } catch (e) {}
                try {
                    const parent = input.parentElement;
                    if (parent) {
                        const sib = parent.querySelector('label');
                        if (sib) return sib;
                    }
                } catch (e) {}
                try {
                    if (input.id) {
                        const byFor = document.querySelector('label[for="' + CSS.escape(input.id) + '"]');
                        if (byFor) return byFor;
                    }
                } catch (e) {}
                return null;
            }

            // 3. Lock a label up and watch for any framework attempt to
            //    unfloat it. Runs once per input.
            function lockInput(input) {
                if (!input || input.dataset.floatingLabelLocked === '1') return;
                input.dataset.floatingLabelLocked = '1';

                const label = findFloatingLabel(input);
                if (!label) return;
                label.dataset.floatingLabelLocked = '1';

                // Force initial state.
                label.style.transform = 'translateY(-100%)';
                label.style.webkitTransform = 'translateY(-100%)';
                label.style.fontSize = '0.75em';
                label.style.top = '0px';
                label.style.opacity = '1';

                // 4. MutationObserver: re-assert the lock if anything tries to
                //    remove the data attribute or change the transform/size.
                const observer = new MutationObserver(function(mutations) {
                    for (let i = 0; i < mutations.length; i++) {
                        const m = mutations[i];
                        if (m.type !== 'attributes') continue;
                        const t = m.target;
                        if (m.attributeName === 'data-floating-label-locked' &&
                            t.getAttribute('data-floating-label-locked') !== '1') {
                            t.setAttribute('data-floating-label-locked', '1');
                        } else if (m.attributeName === 'style') {
                            if (t.style.transform !== 'translateY(-100%)') {
                                t.style.transform = 'translateY(-100%)';
                            }
                            if (t.style.webkitTransform !== 'translateY(-100%)') {
                                t.style.webkitTransform = 'translateY(-100%)';
                            }
                            if (t.style.fontSize !== '0.75em') {
                                t.style.fontSize = '0.75em';
                            }
                        } else if (m.attributeName === 'class') {
                            // Some frameworks remove a "float-up" class on
                            // blur. If we detect that, force it back.
                            // We don't know the framework class names, so we
                            // just re-set the data attribute which our CSS
                            // rule keys on.
                            t.setAttribute('data-floating-label-locked', '1');
                        }
                    }
                });
                try {
                    observer.observe(label, {
                        attributes: true,
                        attributeFilter: ['style', 'class', 'data-floating-label-locked']
                    });
                } catch (e) {}

                // 5. Stub the input's blur handler so frameworks that listen
                //    to blur to "unfloat" the label never fire. We use
                //    addEventListener with a high-priority capture-phase
                //    listener and immediately stopPropagation, which is
                //    cheaper than mutating framework internals.
                try {
                    input.addEventListener('blur', function(e) {
                        e.stopImmediatePropagation();
                        // Re-assert in case any sibling listener ran first.
                        label.dataset.floatingLabelLocked = '1';
                        label.style.transform = 'translateY(-100%)';
                        label.style.webkitTransform = 'translateY(-100%)';
                    }, true);
                } catch (e) {}
            }

            // 6. Lock as soon as the field receives focus so the label is
            //    already in the floated-up state before any framework animation
            //    can slide it back down.
            function onFocusIn(e) {
                const t = e.target;
                if (!t) return;
                if (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA') return;
                if (t.type === 'hidden' || t.type === 'submit' ||
                    t.type === 'button' || t.type === 'checkbox' ||
                    t.type === 'radio' || t.type === 'file') return;
                lockInput(t);
            }
            document.addEventListener('focusin', onFocusIn, true);

            // 7. Lock on first input event (user typed at least one char).
            function onFirstInput(e) {
                const t = e.target;
                if (!t) return;
                if (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA') return;
                if (t.type === 'hidden' || t.type === 'submit' ||
                    t.type === 'button' || t.type === 'checkbox' ||
                    t.type === 'radio' || t.type === 'file') return;
                if (typeof t.value === 'string' && t.value.length > 0) {
                    lockInput(t);
                }
            }
            document.addEventListener('input', onFirstInput, true);

            // 8. Also lock on the first printable keydown. Some frameworks
            //    animate on focus and don't fire 'input' until after the
            //    animation, so locking on keydown ensures the label is
            //    already locked by the time the framework's animation
            //    completion handler runs.
            function onFirstKey(e) {
                const t = e.target;
                if (!t) return;
                if (t.tagName !== 'INPUT' && t.tagName !== 'TEXTAREA') return;
                // Only printable single-character keys (ignore Shift, Ctrl, etc.)
                if (!e.key || e.key.length !== 1) return;
                if (typeof t.value === 'string' && t.value.length > 0) {
                    lockInput(t);
                }
            }
            document.addEventListener('keydown', onFirstKey, true);

            // 8. Lock any input that already has content on page load
            //    (autofill restore, pre-filled forms).
            function lockPreFilled() {
                const inputs = document.querySelectorAll('input, textarea');
                for (let i = 0; i < inputs.length; i++) {
                    const el = inputs[i];
                    if (el.value && el.value.length > 0) {
                        lockInput(el);
                    }
                }
            }
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', lockPreFilled, { once: true });
            } else {
                lockPreFilled();
            }
        })();
        """
        try:
            await context.add_init_script(script)
            logger.debug(f"[FloatingLabel] Applied floating-label lock for session {session_id}")
        except Exception as e:
            logger.warning(f"[FloatingLabel] Failed to apply for session {session_id}: {e}")

    async def _apply_webauthn_disable(self, context, session_id: str):
        """
        HARD disable Microsoft/WebAuthn passkey prompts.
        Method: Inject WebAuthn override + CDP virtual authenticator to prevent OS-level prompt.
        
        This prevents:
        - Windows Hello / Microsoft passkey popup
        - Browser passkey UI
        - Sites will fallback to password login automatically
        
        Applied to all pages in context for Microsoft sites compatibility.
        """
        try:
            # 1. BLOCK WebAuthn API at JS level (prevents sites like Microsoft from triggering passkey flow)
            from webauthn_block import WEBAUTHN_BLOCK_JS, VIRTUAL_AUTHENTICATOR_OPTIONS
            webauthn_block_script = WEBAUTHN_BLOCK_JS
            
            await context.add_init_script(webauthn_block_script)
            logger.debug(f"[WebAuthn] Applied JS blocking script for session {session_id}")
            
            # 2. ADD virtual authenticator via CDP (prevents native OS passkey popup entirely)
            # This creates a fake CTAP2 authenticator that returns empty results
            try:
                # Get first page to establish CDP session
                if context.pages:
                    page = context.pages[0]
                    cdp_session = await context.new_cdp_session(page)
                    
                    # Enable WebAuthn CDP support
                    await cdp_session.send("WebAuthn.enable")
                    
                    # Add virtual authenticator with ctap2 protocol
                    # This makes Chrome think there's an authenticator, but it returns empty results
                    await cdp_session.send("WebAuthn.addVirtualAuthenticator", {
                        "options": VIRTUAL_AUTHENTICATOR_OPTIONS
                    })
                    
                    logger.debug(f"[WebAuthn] CDP virtual authenticator added for session {session_id}")
                    
                    # Close CDP session
                    await cdp_session.detach()
            except Exception as cdp_error:
                # CDP method might not be available on all systems
                logger.warning(f"[WebAuthn] CDP method failed (may not be supported): {cdp_error}")
            
            logger.debug(f"[WebAuthn] WebAuthn/passkey blocking applied for session {session_id}")
            
        except Exception as e:
            logger.error(f"[WebAuthn] Error applying WebAuthn disable: {e}")


class SessionStorage:
    """Manages session storage and state persistence"""
    
    def __init__(self, config):
        self.config = config
        self.base_dir = Path("profiles")
        self.base_dir.mkdir(exist_ok=True)
    
    def _get_profile_path(self, session_id: str, domain: str) -> Path:
        """Get profile directory for session"""
        safe_domain = domain.replace('.', '_').replace(':', '_')
        return self.base_dir / f"{session_id}_{safe_domain}"
    
    async def save_session(self, session_id: str, context, domain: str):
        """Save session state"""
        try:
            profile_path = self._get_profile_path(session_id, domain)
            profile_path.mkdir(exist_ok=True)
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    async def load_session(self, session_id: str, context, domain: str):
        """Load session state"""
        try:
            profile_path = self._get_profile_path(session_id, domain)
            if profile_path.exists():
                pass
        except Exception as e:
                logger.error(f"[Profile Error] {e}")
    
    def get_available_sessions(self, domain: str = None) -> list:
        """Get list of available sessions"""
        sessions = []
        if self.base_dir.exists():
            for d in self.base_dir.iterdir():
                if d.is_dir():
                    sessions.append({
                        'id': d.name,
                        'path': str(d)
                    })
        return sessions
