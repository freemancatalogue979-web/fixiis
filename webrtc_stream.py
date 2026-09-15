"""
WebRTC Stream - High-performance streaming using WebRTC
Replaces WebSocket binary protocol with WebRTC video track for lower latency
and better performance. CDP screencast still captures frames, but they're
delivered through a video track instead of raw bytes over WebSocket.

Key design:
- Frame capture: CDP Page.startScreencast (same as POC.py)
- Frame transport: WebRTC video track (replaces raw WebSocket binary)
- Signaling: SDP offer/answer via the existing WebSocket control channel
- Input handling: Still via WebSocket (no change)
"""

import asyncio
import base64
import time
import logging
from typing import Optional, Callable, Any, Dict
from dataclasses import dataclass, field

# Patch aioice STUN transaction timeout issue early
try:
    import aioice_patch
except ImportError:
    pass

logger = logging.getLogger(__name__)


# aiortc is the WebRTC implementation we use. It's already in requirements.txt
try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCConfiguration, RTCIceServer
    from av import VideoFrame
    AIORTC_AVAILABLE = True
except ImportError as e:
    AIORTC_AVAILABLE = False
    logger.error(f"aiortc/av not available: {e}. WebRTC streaming will be disabled.")

@dataclass
class WebRTCConfig:
    """Configuration for WebRTC streaming"""
    # Capture method - "cdp" uses CDP screencast, "screenshot" uses Playwright screenshot
    method: str = "cdp"

    # Frame format - PNG for lossless capture
    cdp_format: str = "png"
    cdp_quality: int = 100   # High quality 100

    # Target FPS
    target_fps: int = 60

    # Capture dimensions (calculated from viewport)
    capture_width: int = 0
    capture_height: int = 0

    # Frame queue size - keep minimal for real-time interactive responsiveness (no lag buffer)
    max_queue_size: int = 2

    # Watchdog threshold (seconds) - if no frames for this long, restart
    watchdog_timeout: float = 4.0  # More tolerant for smooth playback

    # Page reload after this many consecutive watchdog failures
    watchdog_reload_after: int = 3  # Wait longer before harsh reload

    # ICE servers for NAT traversal (use multiple for redundancy)
    ice_servers: list = field(default_factory=lambda: [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun2.l.google.com:19302"},
        {"urls": "stun:stun3.l.google.com:19302"},
        {"urls": "stun:stun4.l.google.com:19302"},
    ])

    # Frame acquisition timeout (prevent hangs) - RELAXED for smooth playback
    frame_acquire_timeout: float = 0.1  # 100ms - more tolerant

    # Frame pacing - smoothing for jitter
    enable_frame_smoothing: bool = True  # Smooth out timing jitter
    
    # Debug logging
    debug: bool = False


def _normalize_webrtc_config_dict(config: Dict[str, Any]) -> WebRTCConfig:
    """Normalize a configuration dictionary into a WebRTCConfig dataclass."""
    normalized = {}
    for key, value in config.items():
        if key == 'iceServers':
            normalized['ice_servers'] = value
        elif key == 'targetFps':
            normalized['target_fps'] = value
        elif key == 'captureWidth':
            normalized['capture_width'] = value
        elif key == 'captureHeight':
            normalized['capture_height'] = value
        elif key == 'maxQueueSize':
            normalized['max_queue_size'] = value
        elif key == 'watchdogTimeout':
            normalized['watchdog_timeout'] = value
        elif key == 'watchdogReloadAfter':
            normalized['watchdog_reload_after'] = value
        elif key == 'frameAcquireTimeout':
            normalized['frame_acquire_timeout'] = value
        elif key == 'enableFrameSmoothing':
            normalized['enable_frame_smoothing'] = value
        else:
            normalized[key] = value
    valid_keys = set(WebRTCConfig.__dataclass_fields__.keys())
    normalized = {k: v for k, v in normalized.items() if k in valid_keys}
    return WebRTCConfig(**normalized)


class CDPVideoTrack(VideoStreamTrack):
    """
    Custom VideoStreamTrack that pulls frames from an asyncio.Queue
    and yields them as VideoFrame objects for WebRTC transmission.
    """

    kind = "video"

    def __init__(self, frame_queue: asyncio.Queue, framerate: int = 30):
        super().__init__()
        self.frame_queue = frame_queue
        self.framerate = framerate
        self._frame_count = 0
        self._last_log_time = 0.0
        self._last_frame_buffer = None  # Keep last frame for reuse (no blanks)

    async def recv(self):
        """
        Get next frame for transmission. Non-blocking, always returns immediately.
        Drains queue to latest frame so latency stays at real-time (sub-frame).
        """
        import io
        import numpy as np

        pts, time_base = await self.next_timestamp()

        # Drain queue to get the absolute freshest frame without latency build-up
        jpeg_or_png_data = None
        while True:
            try:
                jpeg_or_png_data = self.frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        frame = None
        if jpeg_or_png_data is not None:
            try:
                import cv2
                arr = np.frombuffer(jpeg_or_png_data, dtype=np.uint8)
                img_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img_bgr is not None:
                    frame = VideoFrame.from_ndarray(img_bgr, format="bgr24")
                    self._frame_count += 1
            except Exception:
                pass

            if frame is None:
                try:
                    from PIL import Image
                    img = Image.open(io.BytesIO(jpeg_or_png_data))
                    img_rgb = np.array(img.convert("RGB"))
                    frame = VideoFrame.from_ndarray(img_rgb, format="rgb24")
                    self._frame_count += 1
                except Exception:
                    frame = None

        # If no frame available, reuse last frame to prevent blanks
        if frame is None:
            if self._last_frame_buffer is not None:
                frame = self._last_frame_buffer
            else:
                # First frame - use blank
                blank = np.zeros((720, 1280, 3), dtype=np.uint8)
                frame = VideoFrame.from_ndarray(blank, format="rgb24")
        else:
            # Save this frame for reuse
            self._last_frame_buffer = frame

        frame.pts = pts
        frame.time_base = time_base
        return frame


class WebRTCStreamer:
    """
    WebRTC-based streamer.

    Lifecycle:
    1. start(page, websocket) - Attach to a Playwright page, start CDP screencast
       pushing frames into internal queue. The websocket is kept for control
       messages (signaling, input) but NOT used for video transport.
    2. handle_offer(sdp, type) - Receive SDP offer from client, return SDP answer.
       This establishes the WebRTC peer connection with our video track.
    3. add_ice_candidate(candidate) - Optional: trickle ICE candidates.
    4. stop() - Tear down everything.
    """

    def __init__(self, config: Optional[WebRTCConfig] = None):
        if not AIORTC_AVAILABLE:
            raise RuntimeError("aiortc is not installed; WebRTC streaming unavailable")

        if isinstance(config, dict):
            config = _normalize_webrtc_config_dict(config)
        self.config = config or WebRTCConfig()
        self._page = None
        self._websocket = None
        self._pc: Optional[RTCPeerConnection] = None
        self._video_track: Optional[CDPVideoTrack] = None

        # Queue feeding the video track
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.max_queue_size)

        # CDP screencast state
        self._cdp_client = None
        self._screencast_running = False

        # Lifecycle
        self._is_streaming = False
        self._started_at: float = 0.0
        self._last_frame_time: float = 0.0

        # Tasks
        self._watchdog_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._screenshot_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
 
        # Stats
        self._stats = {
            'frames_received': 0,
            'frames_dropped': 0,
            'frames_sent': 0,
            'screencast_restarts': 0,
            'webrtc_state': 'new',
        }

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self, page, websocket: Any) -> bool:
        """
        Start the streamer. Attaches to a page, begins CDP screencast.
        The actual WebRTC connection is established later via handle_offer.

        Args:
            page: Playwright page object
            websocket: WebSocket connection (used for signaling + input only)

        Returns:
            True if screencast started successfully
        """
        try:
            if not page or page.is_closed():
                logger.error("Cannot start WebRTC stream: page is closed")
                return False

            self._page = page
            self._websocket = websocket
            self._is_streaming = True
            self._started_at = time.time()
            self._shutdown_event.clear()
            self._last_frame_time = time.time()

            # Compute capture dimensions from viewport if not set
            if self.config.capture_width <= 0 or self.config.capture_height <= 0:
                # Will be updated later when we know the actual viewport
                # (the session calls update_viewport after start)
                self.config.capture_width = 1280
                self.config.capture_height = 720
 
            # Start frame source based on method
            method = getattr(self.config, 'method', '').lower()
            browser_name = None
            if self._page and getattr(self._page, 'context', None):
                context = self._page.context
                browser_name = getattr(context, '_browser_name', None)
                if not browser_name:
                    browser_obj = getattr(context, 'browser', None)
                    browser_type = getattr(browser_obj, 'browser_type', None)
                    browser_name = getattr(browser_type, 'name', None) if browser_type else None
            # Firefox screenshot fallback removed — unified CDP screencast for all live views.
            # Keep method as-is even on Firefox; CDP now handles both desktop/mobile via screencast.
            if browser_name and isinstance(browser_name, str) and browser_name.lower() == 'firefox' and method != 'screenshot':
                logger.debug(f"Firefox detected but keeping CDP method={method} (screenshot fallback disabled per admin request)")
            logger.debug(f"WebRTCStreamer starting with method={method} capture={self.config.capture_width}x{self.config.capture_height}")
            if method == 'screenshot':
                ok = await self._start_screenshot_capture()
                if not ok:
                    logger.error("Failed to start screenshot capture for WebRTC stream")
                    return False
            else:
                ok = await self._start_cdp_screencast()
                if not ok:
                    logger.error("Failed to start CDP screencast for WebRTC stream")
                    return False

            # Start watchdog (auto-restart on stall + page reload on persistent failure)
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

            # Start keepalive that pings client (so WebRTC NAT bindings stay warm)
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

            return True

        except Exception as e:
            logger.error(f"WebRTC streamer start failed: {e}")
            await self.stop()
            return False

    # ------------------------------------------------------------------ #
    # Admin monitoring compatibility
    # ------------------------------------------------------------------ #
    #
    # session.py calls enable_frame_callback()/set_frame_callback() on
    # self._streamer. Those methods exist on CDPStreamer (binary WebSocket
    # protocol) but not on WebRTCStreamer - admin monitoring for WebRTC
    # subscribes to the video track directly via the signaling channel,
    # so these are no-ops here. Defined only to keep session.py happy.

    def enable_frame_callback(self, enabled: bool = True) -> None:
        """No-op for WebRTCStreamer (admin uses video-track subscription)."""
        return None

    def set_frame_callback(self, callback) -> None:
        """No-op for WebRTCStreamer (admin uses video-track subscription)."""
        self._admin_frame_callback = callback
        return None

    async def stop(self):
        """Stop streaming, close all connections and tasks."""
        logger.debug("Stopping WebRTC streamer")
        self._is_streaming = False
        self._shutdown_event.set()

        # Cancel tasks
        for task in (self._watchdog_task, self._keepalive_task, self._screenshot_task):
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        self._watchdog_task = None
        self._keepalive_task = None
        self._screenshot_task = None

        # Stop CDP screencast
        await self._stop_cdp_screencast()

        # Close WebRTC peer connection
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass
        self._pc = None
        self._video_track = None

        self._page = None
        self._websocket = None
        logger.debug("WebRTC streamer stopped")

    # ------------------------------------------------------------------ #
    # WebRTC signaling
    # ------------------------------------------------------------------ #

    async def handle_offer(self, sdp: str, sdp_type: str = "offer") -> Dict:
        """
        Process SDP offer from client, return SDP answer.

        Args:
            sdp: SDP offer string from client
            sdp_type: SDP type, should be "offer"

        Returns:
            Dict with {"sdp": answer_sdp, "type": "answer"}
        """
        try:
            # Create peer connection if not yet created
            if self._pc is None:
                # Create the peer connection with configured ICE servers so remote
                # clients can connect reliably across NAT/firewalls.
                ice_servers = [
                    RTCIceServer(**server) if isinstance(server, dict) else server
                    for server in self.config.ice_servers
                ]
                peer_config = RTCConfiguration(iceServers=ice_servers)
                self._pc = RTCPeerConnection(peer_config)

                # Create the video track and add it
                self._video_track = CDPVideoTrack(
                    frame_queue=self._frame_queue,
                    framerate=self.config.target_fps,
                )
                self._pc.addTrack(self._video_track)

                @self._pc.on("connectionstatechange")
                async def on_connectionstatechange():
                    state = self._pc.connectionState
                    self._stats['webrtc_state'] = state
                    if state == "connected" or state == "completed":
                        logger.debug(f"WebRTC connected successfully")
                    elif state == "failed":
                        logger.warning(f"WebRTC connection failed")
                        try:
                            await self._pc.close()
                        except Exception:
                            pass

            offer = RTCSessionDescription(sdp=sdp, type=sdp_type)
            await self._pc.setRemoteDescription(offer)

            answer = await self._pc.createAnswer()
            await self._pc.setLocalDescription(answer)

            return {
                "sdp": self._pc.localDescription.sdp,
                "type": self._pc.localDescription.type,
            }

        except Exception as e:
            logger.error(f"SDP answer creation failed: {e}")
            raise

    async def add_ice_candidate(self, candidate_data: Dict):
        """
        Add a trickle ICE candidate from the client.

        Args:
            candidate_data: Dict with candidate, sdpMid, sdpMLineIndex
        """
        if not self._pc:
            logger.warning("Cannot add ICE candidate: no peer connection")
            return
        try:
            from aiortc.sdp import candidate_from_sdp
            # aiortc API: use addIceCandidate with RTCIceCandidate init
            await self._pc.addIceCandidate(candidate_data)
        except Exception as e:
            logger.debug(f"add_ice_candidate error (non-fatal): {e}")

    # ------------------------------------------------------------------ #
    # CDP screencast (frame source)
    # ------------------------------------------------------------------ #

    async def _start_cdp_screencast(self) -> bool:
        """Start CDP screencast and push decoded frames into the queue."""
        try:
            if self._cdp_client is not None:
                try:
                    await self._cdp_client.send("Page.stopScreencast")
                except Exception:
                    pass
                try:
                    await self._cdp_client.detach()
                except Exception:
                    pass
                self._cdp_client = None

            self._cdp_client = await self._page.context.new_cdp_session(self._page)
            self._cdp_client.on("Page.screencastFrame", self._on_screencast_frame)
            await self._cdp_client.send("Page.enable")
            await self._cdp_client.send("Page.startScreencast", {
                "format": self.config.cdp_format,
                "quality": self.config.cdp_quality,
                "maxWidth": self.config.capture_width,
                "maxHeight": self.config.capture_height,
                "everyNthFrame": 1,
            })

            # Drain stale frames
            while not self._frame_queue.empty():
                try:
                    self._frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            self._screencast_running = True
            self._last_frame_time = time.time()
            return True

        except Exception as e:
            logger.error(f"CDP screencast start failed: {e}")
            self._cdp_client = None
            return False

    async def _stop_cdp_screencast(self):
        """Stop CDP screencast and clean up the client."""
        self._screencast_running = False
        if self._cdp_client:
            try:
                await self._cdp_client.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp_client.detach()
            except Exception:
                pass
        self._cdp_client = None

    async def _start_screenshot_capture(self) -> bool:
        """Start screenshot-based frame capture for WebRTC."""
        try:
            if self._screenshot_task and not self._screenshot_task.done():
                self._screenshot_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(self._screenshot_task), timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            self._screencast_running = True
            logger.debug("Starting WebRTC screenshot capture task")
            if self._page and not self._page.is_closed():
                try:
                    await self._page.evaluate('window.scrollTo(0, 0)')
                except Exception:
                    pass
            self._screenshot_task = asyncio.create_task(self._screenshot_capture_loop())
            return True
        except Exception as e:
            logger.error(f"WebRTC screenshot capture start failed: {e}")
            return False
 
    async def _screenshot_capture_loop(self):
        """Capture page screenshots as frames and push them into the queue."""
        from PIL import Image
        import io

        frame_interval = 1.0 / max(1, self.config.target_fps)
        consecutive_errors = 0
        while self._is_streaming and not self._shutdown_event.is_set():
            start_time = time.time()
            try:
                screenshot_kwargs = {
                    'type': 'png',
                    'full_page': False,
                }
                if self.config.capture_width > 0 and self.config.capture_height > 0:
                    screenshot_kwargs['clip'] = {
                        'x': 0,
                        'y': 0,
                        'width': self.config.capture_width,
                        'height': self.config.capture_height,
                    }
                if getattr(self._page.context, '_browser_name', '').lower() == 'firefox':
                    screenshot_kwargs['scale'] = 'device'
                logger.debug(f"WebRTC screenshot capture kwargs: {screenshot_kwargs}")
                try:
                    screenshot = await asyncio.wait_for(
                        self._page.screenshot(**screenshot_kwargs),
                        timeout=10.0
                    )
                except TypeError:
                    screenshot_kwargs.pop('scale', None)
                    screenshot = await asyncio.wait_for(
                        self._page.screenshot(**screenshot_kwargs),
                        timeout=10.0
                    )
                except Exception as e:
                    logger.warning(f"WebRTC screenshot capture failed: {e}")
                    consecutive_errors += 1
                    await asyncio.sleep(0.25)
                    continue
                if screenshot:
                    try:
                        img = Image.open(io.BytesIO(screenshot))
                        if self.config.method != 'screenshot':
                            if img.width != self.config.capture_width or img.height != self.config.capture_height:
                                img = img.resize((self.config.capture_width, self.config.capture_height), Image.LANCZOS)
                                buf = io.BytesIO()
                                img.save(buf, format='PNG')
                                screenshot = buf.getvalue()
                                logger.debug(
                                    f"WebRTC screenshot resized to {self.config.capture_width}x{self.config.capture_height}"
                                )
                        else:
                            # Preserve the native screenshot output: the
                            # capture dimensions already match the CSS
                            # viewport 1:1, so resizing here would only add
                            # blur.
                            if img.width <= 0 or img.height <= 0:
                                logger.warning("WebRTC screenshot returned invalid dimensions")

                    except Exception as e:
                        logger.debug(f"Failed to normalize screenshot size: {e}")
                    self._push_frame(screenshot)
                    self._last_frame_time = time.time()
                    self._stats['frames_received'] += 1
                    logger.debug(f"WebRTC screenshot pushed frame len={len(screenshot)}")
                    consecutive_errors = 0
            except asyncio.TimeoutError:
                logger.warning("WebRTC screenshot frame timeout")
                consecutive_errors += 1
            except Exception as e:
                error_str = str(e)
                if any(x in error_str for x in ['TargetClosedError', 'target closed', 'page has been closed', 'context has been closed']):
                    break
                logger.error(f"WebRTC screenshot frame error: {e}")
                consecutive_errors += 1
            if consecutive_errors >= 10:
                logger.error("WebRTC screenshot capture failed repeatedly, stopping")
                break
            elapsed = time.time() - start_time
            await asyncio.sleep(max(0, frame_interval - elapsed))

    def _push_frame(self, frame_bytes: bytes):
        """Push a raw frame into the video queue without blocking."""
        try:
            self._frame_queue.put_nowait(frame_bytes)
        except asyncio.QueueFull:
            try:
                self._frame_queue.get_nowait()
                self._stats['frames_dropped'] += 1
                self._frame_queue.put_nowait(frame_bytes)
            except asyncio.QueueEmpty:
                pass

    def _on_screencast_frame(self, frame_data: Dict[str, Any]):
        """
        CDP screencast frame callback. Push to queue IMMEDIATELY without blocking.
        Acknowledge immediately to prevent Chrome from throttling frame delivery.
        """
        try:
            image_data = frame_data.get("data", "")
            session_id = frame_data.get("sessionId", "")

            if not image_data or not session_id:
                return

            # Decode base64 -> bytes (fast operation)
            if isinstance(image_data, str):
                frame_bytes = base64.b64decode(image_data)
            else:
                frame_bytes = image_data

            self._last_frame_time = time.time()
            self._stats['frames_received'] += 1

            # Non-blocking put - no waiting on queue
            try:
                self._frame_queue.put_nowait(frame_bytes)
            except asyncio.QueueFull:
                # Queue full - drop oldest to make room (happens instantly)
                try:
                    self._frame_queue.get_nowait()
                    self._stats['frames_dropped'] += 1
                    # Now put the new frame
                    self._frame_queue.put_nowait(frame_bytes)
                except asyncio.QueueEmpty:
                    pass

            # Acknowledge IMMEDIATELY with fire-and-forget (no await, no delay)
            if self._cdp_client and self._screencast_running:
                # Create fire-and-forget task - doesn't block frame processing
                asyncio.create_task(self._ack_frame_fast(session_id))

        except Exception as e:
            if self.config.debug:
                logger.debug(f"_on_screencast_frame error: {e}")

    async def _ack_frame_fast(self, session_id: str):
        """Fast frame ack - fire and forget, doesn't block"""
        try:
            if self._cdp_client and self._screencast_running:
                await self._cdp_client.send("Page.screencastFrameAck", {
                    "sessionId": session_id
                })
        except Exception:
            pass  # Silent - ack failures don't matter

    # ------------------------------------------------------------------ #
    # Watchdog & keepalive
    # ------------------------------------------------------------------ #

    async def _watchdog_loop(self):
        """
        Watch for stalled screencast. Restart on stall; reload page on
        persistent failure (typical cause: tab was backgrounded/throttled).
        Improved with better timing and faster response.
        """
        consecutive_failures = 0
        last_heartbeat = time.time()
        check_count = 0

        while self._is_streaming and not self._shutdown_event.is_set():
            try:
                # Check every 500ms instead of 1s for faster detection
                await asyncio.sleep(0.5)
                check_count += 1

                now = time.time()
                gap = now - self._last_frame_time if self._last_frame_time else 0

                if gap > self.config.watchdog_timeout and self._screencast_running:
                    consecutive_failures += 1
                    self._stats['screencast_restarts'] += 1
                    # Only log high-level issues, not verbose queue info
                    if consecutive_failures == 1:
                        logger.warning(f"[watchdog] Frame stall detected ({gap:.1f}s), restarting...")
 
                    # Reload page if simple restarts aren't working
                    if consecutive_failures >= self.config.watchdog_reload_after:
                        logger.warning("[watchdog] Multiple restarts needed, reloading page...")
                        try:
                            if self._page and not self._page.is_closed():
                                # Reload with faster timeout
                                await self._page.reload(
                                    wait_until="domcontentloaded", timeout=15000
                                )
                                # Re-attach to the new page
                                await asyncio.sleep(1)  # Reduced from 2
                                # Try to play video if present
                                try:
                                    await self._page.evaluate(
                                        "document.querySelector('video')?.play().catch(()=>{})"
                                    )
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.warning(f"[watchdog] reload failed: {e}")
                        consecutive_failures = 0
 
                    method = getattr(self.config, 'method', '').lower()
                    if method == 'screenshot':
                        await self._stop_cdp_screencast()
                        await asyncio.sleep(0.2)
                        ok = await self._start_screenshot_capture()
                        if not ok:
                            logger.error("[watchdog] screenshot capture restart failed")
                    else:
                        await self._stop_cdp_screencast()
                        await asyncio.sleep(0.2)
                        ok = await self._start_cdp_screencast()
                        if not ok:
                            logger.error("[watchdog] screencast restart failed")
                else:
                    consecutive_failures = 0

                # Tiny no-op scroll every 30s to keep the tab "active" and prevent throttling
                if now - last_heartbeat > 30:
                    last_heartbeat = now
                    try:
                        if self._page and not self._page.is_closed():
                            await self._page.evaluate(
                                "window.scrollBy(0,1); window.scrollBy(0,-1);"
                            )
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"watchdog error: {e}")
                await asyncio.sleep(1.0)

    async def _keepalive_loop(self):
        """
        Periodically ping the client over the WebSocket to keep
        NAT bindings warm and detect dead connections early.
        """
        while self._is_streaming and not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(15.0)
                if self._websocket and self._is_streaming:
                    try:
                        if hasattr(self._websocket, 'client_state'):
                            from websockets.protocol import State
                            if self._websocket.client_state != State.CONNECTED:
                                continue
                        # Fire-and-forget - we don't want a slow send to stall the loop
                        await self._websocket.send_json({
                            "type": "webrtc_keepalive",
                            "stats": self.get_stats(),
                        })
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Configuration helpers
    # ------------------------------------------------------------------ #

    async def update_viewport(self, width: int, height: int):
        """Update capture dimensions and restart screencast with new size."""
        if width <= 0 or height <= 0:
            return

        # Avoid restart if size hasn't materially changed
        if (abs(width - self.config.capture_width) < 5 and
                abs(height - self.config.capture_height) < 5):
            return

        self.config.capture_width = width
        self.config.capture_height = height

        if self._screencast_running and self._cdp_client:
            try:
                await self._cdp_client.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp_client.send("Page.startScreencast", {
                    "format": self.config.cdp_format,
                    "quality": self.config.cdp_quality,
                    "maxWidth": width,
                    "maxHeight": height,
                    "everyNthFrame": 1,
                })
                self._last_frame_time = time.time()
                logger.debug(f"WebRTC screencast resized to {width}x{height}")
            except Exception as e:
                logger.warning(f"update_viewport failed, restarting screencast: {e}")
                await self._stop_cdp_screencast()
                await self._start_cdp_screencast()

    def get_stats(self) -> Dict[str, Any]:
        """Get streaming statistics."""
        elapsed = time.time() - self._started_at if self._started_at else 0
        return {
            'method': 'webrtc',
            'cdp_format': self.config.cdp_format,
            'capture_resolution': f"{self.config.capture_width}x{self.config.capture_height}",
            'target_fps': self.config.target_fps,
            'frames_received': self._stats['frames_received'],
            'frames_dropped': self._stats['frames_dropped'],
            'screencast_restarts': self._stats['screencast_restarts'],
            'queue_size': self._frame_queue.qsize(),
            'queue_max': self.config.max_queue_size,
            'webrtc_state': self._stats['webrtc_state'],
            'uptime_seconds': round(elapsed, 1),
            'actual_fps': round(self._stats['frames_received'] / max(elapsed, 1), 1),
        }

    def get_viewport_dimensions(self) -> Dict[str, int]:
        """Return current capture dimensions - used by session for resize logic."""
        return {
            'width': self.config.capture_width,
            'height': self.config.capture_height,
        }

    @property
    def is_active(self) -> bool:
        return self._is_streaming

    @property
    def webrtc_ready(self) -> bool:
        """True once a client has connected via WebRTC and the connection is live."""
        if not self._pc:
            return False
        return self._pc.connectionState in ("connected", "completed")


def create_webrtc_streamer(method: Optional[str] = None, config: Optional[WebRTCConfig] = None) -> WebRTCStreamer:
    """Factory function - mirrors create_streamer() in cdp_screencast_stream.py"""
    if isinstance(config, dict):
        config = _normalize_webrtc_config_dict(config)
    if config is None:
        config = WebRTCConfig()
    if method:
        config.method = method
    return WebRTCStreamer(config)
