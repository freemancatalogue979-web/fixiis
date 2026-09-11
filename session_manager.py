"""
Session Manager - Global session management and orchestration
MODIFIED FOR V7: Removed singleton enforcement per profile
Allows multiple concurrent sessions and impersonations without conflicts
"""

import asyncio
import time
import uuid
import traceback
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from collections import deque
import logging

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Session statistics"""
    def __init__(self):
        self.total_sessions = 0
        self.active_sessions = 0
        self.paused_sessions = 0
        self.total_frames = 0
        self.start_time = time.time()
        self.frame_history = deque(maxlen=100)
        self.session_history = deque(maxlen=50)
        self.reconnect_count = 0
        self.forced_shutdowns = 0

    def record_frame(self):
        """Record a processed frame"""
        self.total_frames += 1
        self.frame_history.append(time.time())

    def record_reconnect(self):
        """Record a successful reconnect"""
        self.reconnect_count += 1

    def record_forced_shutdown(self):
        """Record a forced shutdown"""
        self.forced_shutdowns += 1

    def get_fps(self) -> float:
        """Get current overall FPS"""
        now = time.time()
        recent_frames = sum(1 for t in self.frame_history if now - t < 1.0)
        return recent_frames


class SessionRegistry:
    """
    V7 Session Registry - WITH SESSION LOCKING
    Enforces that each user_id can only have ONE active session at a time.
    Uses PER-USER locks to allow concurrent operations on different users.
    """

    def __init__(self):
        self._sessions: Dict[str, Dict] = {}  # session_id -> session_data
        self._profile_sessions: Dict[str, List[str]] = {}  # profile_id -> [session_ids]
        self._websocket_sessions: Dict[str, str] = {}  # websocket_id -> session_id
        self._session_locks: Dict[str, str] = {}  # user_id -> session_id (locked sessions)
        # PER-USER LOCKS: Allows concurrent operations on different users
        self._user_locks: Dict[str, asyncio.Lock] = {}  # Per-user locks
        self._locks_lock = asyncio.Lock()  # Lock for managing the locks dictionary
        self._cleanup_task = None

    async def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        """Get or create a per-user lock"""
        async with self._locks_lock:
            if user_id not in self._user_locks:
                self._user_locks[user_id] = asyncio.Lock()
            return self._user_locks[user_id]

    async def _release_user_lock(self, user_id: str):
        """Remove a user lock when no longer needed"""
        async with self._locks_lock:
            if user_id in self._user_locks:
                del self._user_locks[user_id]

    async def start(self):
        """Start the cleanup task"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """Stop the cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

    async def is_session_locked(self, user_id: str) -> bool:
        """Check if a user_id has a locked session"""
        return user_id in self._session_locks

    async def get_locked_session_id(self, user_id: str) -> Optional[str]:
        """Get the session_id that is locked for this user_id"""
        return self._session_locks.get(user_id)

    async def lock_session(self, session_id: str, user_id: str) -> bool:
        """
        Lock a session for a user_id.
        Returns True if successfully locked, False if already locked by another session.
        Uses per-user lock to allow concurrent lock operations on different users.
        """
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            # Check if user_id already has a locked session
            if user_id in self._session_locks:
                existing_session_id = self._session_locks[user_id]
                if existing_session_id != session_id:
                    # Already locked by another session
                    return False

            # Lock this session for the user_id
            self._session_locks[user_id] = session_id
            return True

    async def unlock_session(self, user_id: str) -> bool:
        """Unlock a session for a user_id (called when session closes)"""
        user_lock = await self._get_user_lock(user_id)
        async with user_lock:
            if user_id in self._session_locks:
                del self._session_locks[user_id]
                return True
            return False

    async def create_session(self, session_id: str, profile_id: str, websocket_id: str,
                            metadata: Optional[Dict] = None) -> Optional[Dict]:
        """
        Create a new session WITH singleton enforcement (session locking).
        Only ONE session can be active per user_id at a time.
        Uses per-user lock for better concurrency.
        """
        user_lock = await self._get_user_lock(profile_id)
        async with user_lock:
            # Check if this user_id already has a locked session
            if profile_id in self._session_locks:
                existing_session_id = self._session_locks[profile_id]
                if existing_session_id != session_id:
                    # Session is already locked by another session_id - return None to prevent creation
                    return None

            session_data = {
                'session_id': session_id,
                'profile_id': profile_id,
                'websocket_id': websocket_id,
                'created_at': time.time(),
                'last_active': time.time(),
                'is_active': True,
                'metadata': metadata or {}
            }

            self._sessions[session_id] = session_data

            # Add to profile sessions list
            if profile_id not in self._profile_sessions:
                self._profile_sessions[profile_id] = []
            self._profile_sessions[profile_id].append(session_id)

            # Map websocket to session
            self._websocket_sessions[websocket_id] = session_id

            # Lock this session for the user_id
            self._session_locks[profile_id] = session_id

            return session_data
    
    async def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session by ID"""
        return self._sessions.get(session_id)
    
    async def get_session_by_websocket(self, websocket_id: str) -> Optional[Dict]:
        """Get session by websocket ID"""
        session_id = self._websocket_sessions.get(websocket_id)
        if session_id:
            return await self.get_session(session_id)
        return None
    
    async def get_profile_sessions(self, profile_id: str) -> List[Dict]:
        """Get ALL sessions for a specific profile (no longer just the active one)"""
        session_ids = self._profile_sessions.get(profile_id, [])
        sessions = []
        for session_id in session_ids:
            session = self._sessions.get(session_id)
            if session:
                sessions.append(session)
        return sessions
    
    async def update_activity(self, session_id: str):
        """Update last active timestamp"""
        session = self._sessions.get(session_id)
        if session:
            session['last_active'] = time.time()
    
    async def close_session(self, session_id: str) -> bool:
        """Close and remove a session, and unlock the user_id"""
        session = self._sessions.pop(session_id, None)
        if session:
            profile_id = session.get('profile_id')
            
            # Use per-user lock for cleanup
            if profile_id:
                user_lock = await self._get_user_lock(profile_id)
                async with user_lock:
                    # Remove from profile sessions list
                    if profile_id in self._profile_sessions:
                        try:
                            self._profile_sessions[profile_id].remove(session_id)
                            if not self._profile_sessions[profile_id]:
                                del self._profile_sessions[profile_id]
                        except ValueError:
                            pass

                    # Unlock the session for this user_id
                    if profile_id in self._session_locks:
                        if self._session_locks[profile_id] == session_id:
                            del self._session_locks[profile_id]

            # Remove websocket mapping (needs global lock as it affects _websocket_sessions)
            self._websocket_sessions.pop(session.get('websocket_id'), None)

            session['is_active'] = False
            return True
        return False
    
    async def close_websocket_session(self, websocket_id: str) -> bool:
        """Close session by websocket ID"""
        session_id = self._websocket_sessions.get(websocket_id)
        if session_id:
            return await self.close_session(session_id)
        return False
    
    async def get_active_session_count(self, profile_id: Optional[str] = None) -> int:
        """Get count of active sessions"""
        if profile_id:
            return len(self._profile_sessions.get(profile_id, []))
        return len(self._sessions)
    
    async def get_all_sessions(self) -> List[Dict]:
        """Get all active sessions"""
        return list(self._sessions.values())
    
    async def _cleanup_loop(self):
        """Periodically cleanup inactive sessions - OPTIMIZED for less overhead"""
        while True:
            try:
                await asyncio.sleep(120)  # Increased from 60s to 120s - less CPU usage
                await self._cleanup_dead_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cleanup Error] {e}")
    
    async def _cleanup_dead_sessions(self):
        """Remove sessions that haven't been active for too long"""
        threshold = time.time() - 300  # 5 minutes (cleanup happens after session expires)
        sessions_to_close = []
        
        for session_id, session in self._sessions.items():
            if not session.get('is_active', True) or session.get('last_active', 0) < threshold:
                sessions_to_close.append(session_id)
        
        for session_id in sessions_to_close:
            await self.close_session(session_id)
        
        if sessions_to_close:
            pass  # Silent cleanup


class StreamManager:
    """
    V7 Stream Manager - PER-SESSION LOCKS
    Allows multiple concurrent streams without blocking each other.
    Uses per-session locks for better concurrency.
    """
    
    def __init__(self):
        self._active_streams: Dict[str, str] = {}  # session_id -> status
        self._stream_locks: Dict[str, asyncio.Lock] = {}  # Per-session locks
        self._locks_lock = asyncio.Lock()  # Lock for managing locks dictionary
    
    async def _get_stream_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session lock"""
        async with self._locks_lock:
            if session_id not in self._stream_locks:
                self._stream_locks[session_id] = asyncio.Lock()
            return self._stream_locks[session_id]
    
    async def register_stream(self, session_id: str) -> None:
        """Register a new stream (no blocking - multiple streams allowed)"""
        stream_lock = await self._get_stream_lock(session_id)
        async with stream_lock:
            self._active_streams[session_id] = 'active'
    
    async def unregister_stream(self, session_id: str) -> None:
        """Unregister a stream"""
        stream_lock = await self._get_stream_lock(session_id)
        async with stream_lock:
            self._active_streams.pop(session_id, None)
        # Clean up the lock
        async with self._locks_lock:
            self._stream_locks.pop(session_id, None)
    
    async def get_active_stream_count(self) -> int:
        """Get number of active streams"""
        return len(self._active_streams)
    
    async def get_all_active_streams(self) -> List[str]:
        """Get all active stream session IDs"""
        return list(self._active_streams.keys())


class SessionManager:
    """V7 Session Manager - NO SINGLETON ENFORCEMENT
    
    Key Changes:
    - Removed profile-based singleton enforcement
    - Multiple sessions can share the same profile_id
    - All sessions are independent and can coexist
    - CRITICAL FIX: Per-session locks for true parallelism
    - Session operations are independent - no global bottleneck
    """

    def __init__(self, config, gpu_manager):
        self.config = config
        self.gpu_manager = gpu_manager
        self.sessions: Dict[str, 'NeoStreamingSession'] = {}
        self.stream_manager = StreamManager()  # Multiple streams allowed
        self.registry = SessionRegistry()  # New registry system
        # CRITICAL FIX: Per-session locks instead of global lock
        # This allows sessions to operate in parallel without blocking each other
        self._session_locks: Dict[str, asyncio.Lock] = {}
        self._locks_lock = asyncio.Lock()  # Only for managing the locks dict
        self._sessions_lock = asyncio.Lock()  # Only for adding/removing sessions
        self.stats = SessionStats()
        self.cleanup_task = None
        self._reconnect_tokens: Dict[str, Dict] = {}
        self._pending_remove_tasks: Dict[str, asyncio.Task] = {}
        self._pending_remove_lock = asyncio.Lock()
        
        # Cgroup integration for session memory isolation
        self._session_cgroups: Dict[str, str] = {}
        self._cgroup_manager = None
        self._init_cgroup_manager()
    
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a per-session lock for independent operations"""
        async with self._locks_lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
            return self._session_locks[session_id]
    
    async def _release_session_lock(self, session_id: str):
        """Remove a session lock when session is removed"""
        async with self._locks_lock:
            self._session_locks.pop(session_id, None)

    async def schedule_remove_session(self, session_id: str, delay: float = 30.0, force: bool = False):
        """Schedule delayed session removal to allow quick reconnects."""
        async with self._pending_remove_lock:
            if session_id in self._pending_remove_tasks:
                return
            task = asyncio.create_task(self._delayed_remove_session(session_id, delay, force))
            self._pending_remove_tasks[session_id] = task

    async def cancel_scheduled_removal(self, session_id: str):
        """Cancel a pending session removal if the client reconnects."""
        async with self._pending_remove_lock:
            task = self._pending_remove_tasks.pop(session_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _delayed_remove_session(self, session_id: str, delay: float, force: bool):
        try:
            await asyncio.sleep(delay)
            async with self._pending_remove_lock:
                self._pending_remove_tasks.pop(session_id, None)
            await self.remove_session(session_id, force=force)
        except asyncio.CancelledError:
            pass
    
    def _init_cgroup_manager(self):
        """Initialize cgroup manager if available"""
        try:
            # Import the cgroup manager from main module
            from main import get_cgroup_manager
            self._cgroup_manager = get_cgroup_manager()
            if self._cgroup_manager and self._cgroup_manager._initialized:
                logger.debug("Session manager: Cgroup isolation available")
            else:
                logger.debug("Session manager: Cgroup isolation not available")
        except Exception as e:
            logger.warning(f"Could not initialize cgroup manager: {e}")
            self._cgroup_manager = None
    
    def _create_session_cgroup(self, session_id: str) -> Optional[str]:
        """Create a cgroup for session memory isolation"""
        if not self._cgroup_manager or not self._cgroup_manager._initialized:
            return None
        
        try:
            # Calculate memory limit based on available system memory
            # Give each session 4GB by default, adjust based on total memory
            memory_limit_mb = 4096  # 4GB per session
            
            cgroup_path = self._cgroup_manager.create_session_cgroup(
                session_id, 
                memory_limit_mb=memory_limit_mb
            )
            
            if cgroup_path:
                self._session_cgroups[session_id] = cgroup_path
                logger.debug(f"Created cgroup for session {session_id}")
                return cgroup_path
        except Exception as e:
            logger.warning(f"Could not create cgroup for session {session_id}: {e}")
        
        return None
    
    def _add_browser_to_cgroup(self, session_id: str, browser_pid: int):
        """Add browser process to session's cgroup"""
        if session_id in self._session_cgroups and self._cgroup_manager:
            try:
                self._cgroup_manager.add_process_to_cgroup(session_id, browser_pid)
            except Exception as e:
                logger.warning(f"Could not add browser to cgroup: {e}")
    
    def _cleanup_session_cgroup(self, session_id: str):
        """Clean up session's cgroup"""
        if session_id in self._session_cgroups:
            if self._cgroup_manager:
                self._cgroup_manager.remove_session_cgroup(session_id)
            del self._session_cgroups[session_id]
            logger.debug(f"Cleaned up cgroup for session {session_id}")
        
    async def start(self):
        """Start session manager background tasks"""
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        await self.registry.start()

    async def stop(self):
        """Stop session manager - ENHANCED with per-session locking"""
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        await self.registry.stop()
        
        # Close all sessions - use _sessions_lock for thread-safe access
        async with self._sessions_lock:
            session_ids = list(self.sessions.keys())
        
        # Remove each session (no global lock - each removes its own session)
        for session_id in session_ids:
            await self.remove_session(session_id)

        self._reconnect_tokens.clear()

    async def create_session(self, session_id: str, websocket, user_agent: str,
                             viewport: Dict, pixel_ratio: float,
                             url: str = None, device_id: str = None,
                             user_id: str = None,
                             reconnect_token: str = None,
                             is_impersonation: bool = False,
                             is_mobile: bool = False,
                             client_ip: str = None,
                             country: str = None,
                             state: str = None,
                             city: str = None,
                             zip_code: str = None) -> Optional['NeoStreamingSession']:
        """
        Create a new streaming session - PARALLEL VERSION
        - CRITICAL FIX: No global lock - sessions are independent
        - Resource checking is fast and non-blocking
        - Each session operates in its own context
        - KICK PREVIOUS: Any existing session for the same client_tag (user_id/device_id)
          is force-closed before the new one is created. One tag = one live session.
        """
        # Use user_id for profile management
        profile_user_id = user_id or device_id or session_id
        client_tag = profile_user_id  # Single tag identifier for "one session per tag"

        # Check resources BEFORE lock (fast check)
        can_start, reason = self.gpu_manager.check_resources_available()
        if not can_start:
            logger.warning(f"[Session Create] Cannot create session {session_id}: {reason}")
            return None

        # Check session count limit - use sessions lock only for this
        async with self._sessions_lock:
            if session_id in self.sessions:
                existing_session = self.sessions[session_id]
                logger.debug(f"[Session Create] Reattaching existing session {session_id} for user {profile_user_id}")
                await self.cancel_scheduled_removal(session_id)
                await existing_session.reattach_websocket(websocket)
                self.stats.record_reconnect()
                self._refresh_reconnect_token(session_id, profile_user_id)
                return existing_session

            current_count = len(self.sessions)
            max_sessions = getattr(self.config, 'max_sessions', 20)
            if current_count >= max_sessions:
                logger.warning(f"[Session Create] Cannot create session {session_id}: Max sessions reached ({current_count}/{max_sessions})")
                return None

            # Handle reconnect if token provided
            if reconnect_token and reconnect_token in self._reconnect_tokens:
                token_data = self._reconnect_tokens[reconnect_token]
                if token_data.get('profile_id') == profile_user_id:
                    existing_session_id = token_data.get('session_id')
                    if existing_session_id in self.sessions:
                        logger.debug(f"[Session Create] Reconnecting session {session_id} for user {profile_user_id}")
                        session = self.sessions[existing_session_id]
                        await session.reattach_websocket(websocket)
                        self.stats.record_reconnect()
                        self._refresh_reconnect_token(existing_session_id, profile_user_id)
                        return session

        # KICK PREVIOUS: Force-close any existing session that belongs to the same
        # client_tag (user_id/device_id). One tag => one live session, always.
        # This must happen BEFORE we create the new session.
        await self.kick_previous_sessions(client_tag, exclude_session_id=session_id)

        # Create session OUTSIDE the lock - session creation is independent
        session = None
        try:
            from session import NeoStreamingSession
            logger.debug(f"[Session Create] Creating session {session_id} for user {profile_user_id}")
            
            # Get per-session lock for this session's operations
            session_lock = await self._get_session_lock(session_id)
            
            session = NeoStreamingSession(
                session_id, websocket, user_agent, viewport, pixel_ratio,
                self.config, self.gpu_manager,
                user_id=profile_user_id,
                target_url=url,
                is_mobile=is_mobile,
                client_ip=client_ip,
                country=country,
                state=state,
                city=city,
                zip_code=zip_code
            )
            
            # Store identifiers
            session.device_id = device_id or session_id
            session.user_id = profile_user_id
            session._session_lock = session_lock  # Pass lock to session
            
            # Store client info for notifications
            session.client_ip = client_ip or '-'
            session.country = country or '-'
            session.state = state or '-'
            session.city = city or '-'
            session.zip_code = zip_code or '-'
            session.client_user_agent = user_agent
            
            # Start the session (can take time - doesn't block other sessions)
            success = await session.start(url)
            
            if success:
                # Add to sessions dict
                async with self._sessions_lock:
                    self.sessions[session_id] = session
                
                self.gpu_manager.register_session(session_id, 0)
                self.stats.active_sessions += 1
                self.stats.total_sessions += 1
                
                # Register stream
                await self.stream_manager.register_stream(session_id)
                
                # Create cgroup for session memory isolation
                self._create_session_cgroup(session_id)
                
                # Generate reconnect token
                self._generate_reconnect_token(session_id, profile_user_id)
                
                # Send connect notification (non-blocking)
                asyncio.create_task(self._send_connect_notification({
                    'session_id': session_id,
                    'user_id': profile_user_id,
                    'current_url': url or session.target_url or '',
                    'start_time': time.time(),
                    'ip_address': session.client_ip,
                    'country': session.country,
                    'state': session.state,
                    'city': session.city,
                    'zip': session.zip_code,
                    'user_agent': session.client_user_agent
                }))
                
                max_sessions = getattr(self.config, 'max_sessions', 20)
                logger.debug(f"[Session Create] Session {session_id} created successfully. Active: {len(self.sessions)}/{max_sessions}")
                return session
            else:
                logger.error(f"[Session Create] Session {session_id} start() returned False")
                if session:
                    try:
                        await session.cleanup(force=True)
                    except Exception as cleanup_err:
                        logger.error(f"[Session Create] Cleanup error: {cleanup_err}")
                await self._release_session_lock(session_id)
                return None
                
        except asyncio.CancelledError:
            logger.warning(f"[Session Create] Session {session_id} creation cancelled")
            if session:
                try:
                    await session.cleanup(force=True)
                except Exception:
                    pass
            await self._release_session_lock(session_id)
            raise
        except MemoryError as e:
            logger.error(f"[Session Create] Out of memory creating session {session_id}: {e}")
            self.gpu_manager.cleanup_orphaned_chrome_processes()
            if session:
                try:
                    await session.cleanup(force=True)
                except Exception:
                    pass
            await self._release_session_lock(session_id)
            return None
        except Exception as e:
            logger.error(f"[Session Create] Failed to create session {session_id}: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[Session Create] Traceback: {traceback.format_exc()}")
            if session:
                try:
                    await session.cleanup(force=True)
                except Exception as cleanup_err:
                    logger.error(f"[Session Create] Cleanup error: {cleanup_err}")
            await self._release_session_lock(session_id)
            return None

    def _generate_reconnect_token(self, session_id: str, profile_id: str) -> str:
        """Generate and store a reconnect token for a session"""
        token = str(uuid.uuid4())
        self._reconnect_tokens[token] = {
            'session_id': session_id,
            'profile_id': profile_id,
            'created_at': time.time(),
            'last_used': time.time()
        }
        if session_id in self.sessions:
            self.sessions[session_id].reconnect_token = token
        return token

    def _refresh_reconnect_token(self, session_id: str, profile_id: str) -> Optional[str]:
        """Refresh the reconnect token for an active session"""
        old_token = self._invalidate_reconnect_token(session_id)
        if session_id in self.sessions:
            return self._generate_reconnect_token(session_id, profile_id)
        return None

    def _invalidate_reconnect_token(self, session_id: str) -> Optional[str]:
        """Invalidate the reconnect token for a session"""
        token_to_remove = None
        for token, token_data in self._reconnect_tokens.items():
            if token_data.get('session_id') == session_id:
                token_to_remove = token
                break
        
        if token_to_remove:
            del self._reconnect_tokens[token_to_remove]
            return token_to_remove
        return None

    async def get_session(self, session_id: str) -> Optional['NeoStreamingSession']:
        """Get a session by ID - THREAD-SAFE with per-session lock"""
        # Use per-session lock to safely access this specific session
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            return self.sessions.get(session_id)

    async def get_session_by_user(self, user_id: str) -> List['NeoStreamingSession']:
        """Get ALL sessions for a user (not just one)"""
        user_sessions = []
        for session_id, session in self.sessions.items():
            if session.user_id == user_id:
                user_sessions.append(session)
        return user_sessions

    async def get_session_by_tag(self, client_tag: str) -> List['NeoStreamingSession']:
        """Get all sessions matching a client_tag (= user_id / device_id).
        A client_tag identifies ONE logical client. We enforce one live session per tag.
        """
        if not client_tag:
            return []
        matches = []
        for session_id, session in self.sessions.items():
            sess_user_id = getattr(session, 'user_id', None)
            sess_device_id = getattr(session, 'device_id', None)
            if sess_user_id == client_tag or sess_device_id == client_tag:
                matches.append(session)
        return matches

    async def kick_previous_sessions(self, client_tag: str, exclude_session_id: str = None) -> int:
        """
        Force-close every existing session that belongs to the same client_tag,
        except the one being reconnected (exclude_session_id).

        Behavior:
        - Each previous session's websocket is closed.
        - The browser process is killed (force=True) so resources are released fast.
        - The new session can then start fresh on a clean slate.

        Returns the number of sessions that were kicked.
        """
        if not client_tag:
            return 0

        kicked = 0
        # Snapshot the list under the sessions lock so iteration is safe.
        async with self._sessions_lock:
            candidates = [
                sid for sid, sess in self.sessions.items()
                if sid != exclude_session_id
                and (
                    getattr(sess, 'user_id', None) == client_tag
                    or getattr(sess, 'device_id', None) == client_tag
                )
            ]

        for old_sid in candidates:
            try:
                logger.debug(f"[Kick] Force-closing previous session {old_sid} for client_tag={client_tag}")
                # Capture the previous websocket (if any) so we can drop the connection
                prev_session = self.sessions.get(old_sid)
                prev_ws = getattr(prev_session, 'websocket', None) if prev_session else None

                # Force-remove: kills the browser, releases locks, clears registry
                await self.remove_session(old_sid, force=True)
                kicked += 1

                # Close the old websocket so the previous client gets disconnected.
                # Only attempt graceful close if the WS has been accepted and is still
                # connected - otherwise Starlette raises "Need to call accept first"
                # or a coroutine warning. Wrap in create_task so we never block this
                # loop on a slow/dead client.
                if prev_ws is not None:
                    try:
                        client_state = getattr(prev_ws, 'client_state', None)
                        state_name = getattr(client_state, 'name', None) if client_state else None
                        is_connected = state_name == 'CONNECTED'
                        if is_connected and hasattr(prev_ws, 'close'):
                            asyncio.create_task(self._safe_close_ws(prev_ws))
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[Kick] Failed to close previous session {old_sid}: {e}")

        if kicked:
            logger.debug(f"[Kick] Kicked {kicked} previous session(s) for client_tag={client_tag}")
        return kicked

    @staticmethod
    async def _safe_close_ws(ws):
        """Close a websocket without raising. Used during kick - never awaited
        directly so a slow/dead client cannot stall the new-session creation path."""
        try:
            try:
                await ws.close(code=1000, reason="replaced_by_new_session")
            except Exception:
                pass
        except Exception:
            pass

    async def remove_session(self, session_id: str, force: bool = False):
        """Remove and cleanup a session - ENHANCED with per-session lock
        
        Args:
            session_id: The session ID to remove
            force: If True, forcefully terminates the browser process (for server restart)
        
        CRITICAL FIX: Uses per-session lock so other sessions can operate in parallel
        """
        # Get the per-session lock for this specific session
        session_lock = await self._get_session_lock(session_id)
        
        await self.cancel_scheduled_removal(session_id)
        async with session_lock:
            # Check if session exists
            if session_id not in self.sessions:
                logger.debug(f"[Session Remove] Session {session_id} not found")
                return
            
            session = self.sessions[session_id]
            session_info = {
                'session_id': session_id,
                'user_id': session.user_id if hasattr(session, 'user_id') else '-',
                'target_url': getattr(session, 'target_url', '-'),
                'start_time': getattr(session, 'start_time', 0),
                'current_url': session.get_active_page().url if session.get_active_page() else getattr(session, 'target_url', '-'),
                'ip_address': getattr(session, 'client_ip', '-'),
                'country': getattr(session, 'country', '-'),
                'state': getattr(session, 'state', '-'),
                'city': getattr(session, 'city', '-'),
                'zip': getattr(session, 'zip_code', '-'),
                'user_agent': getattr(session, 'client_user_agent', '-')
            }
            
            logger.debug(f"[Session Remove] Removing session {session_id} (force={force})")
            
            try:
                # Unregister stream FIRST
                await self.stream_manager.unregister_stream(session_id)
            except Exception as e:
                logger.warning(f"[Session Remove] Error unregistering stream: {e}")
            
            try:
                # Clean up registry
                await self.registry.close_session(session_id)
            except Exception as e:
                logger.warning(f"[Session Remove] Error closing registry: {e}")
            
            # Clean up reconnect token
            self._invalidate_reconnect_token(session_id)
            
            # Unregister from GPU manager
            try:
                self.gpu_manager.unregister_session(session_id, 0)
            except Exception as e:
                logger.warning(f"[Session Remove] Error unregistering from GPU manager: {e}")
            
            # Pass force parameter to session cleanup
            try:
                await session.cleanup(force=force)
            except Exception as e:
                logger.warning(f"[Session Remove] Session cleanup error: {e}")
            
            # Remove from sessions dict - use sessions lock for thread-safe removal
            async with self._sessions_lock:
                if session_id in self.sessions:
                    del self.sessions[session_id]
            
            self.stats.active_sessions -= 1
            
            logger.debug(f"[Session Remove] Session {session_id} removed. Active sessions: {len(self.sessions)}")
        
        # Release the session lock and clean up
        await self._release_session_lock(session_id)
        
        # Clean up session cgroup after session removal (outside locks)
        try:
            self._cleanup_session_cgroup(session_id)
        except Exception as e:
            logger.warning(f"[Session Remove] Cgroup cleanup error: {e}")

    async def pause_session(self, user_id: str) -> bool:
        """Pause all sessions for a user"""
        sessions = await self.get_session_by_user(user_id)
        for session in sessions:
            await session.enter_sleep()
            self.stats.paused_sessions += 1
        return len(sessions) > 0

    async def resume_session(self, user_id: str) -> bool:
        """Resume all paused sessions for a user"""
        sessions = await self.get_session_by_user(user_id)
        for session in sessions:
            await session.wake()
            self.stats.paused_sessions -= 1
        return len(sessions) > 0

    async def shutdown_session(self, user_id: str) -> bool:
        """Shutdown all sessions for a user - THREAD-SAFE with per-session locking
        
        CRITICAL FIX: Each session is removed independently without blocking others
        """
        sessions = await self.get_session_by_user(user_id)
        shutdown_count = 0
        for session in sessions:
            await self.remove_session(session.session_id, force=True)
            shutdown_count += 1
        return shutdown_count > 0

    async def get_all_sessions(self) -> List[Dict]:
        """Get info for all active sessions - THREAD-SAFE iteration"""
        sessions_info = []
        
        # Get snapshot of session IDs first (thread-safe)
        async with self._sessions_lock:
            session_ids = list(self.sessions.keys())
        
        # Get info for each session with its own lock
        for session_id in session_ids:
            session = await self.get_session(session_id)
            if session:
                try:
                    info = await session.get_info()
                    sessions_info.append(info)
                except Exception as e:
                    logger.warning(f"[Get All] Error getting info for session {session_id}: {e}")
        
        return sessions_info

    async def close_all_sessions(self, force: bool = False):
        """Close all active sessions - ENHANCED with per-session locking
        
        Args:
            force: If True, forcefully terminates all browser processes (for server restart).
                   When force=True, uses session.cleanup(force=True) to ensure complete termination.
        
        CRITICAL FIX: No global lock - each session removes independently
        """
        logger.debug(f"[Close All] Closing all sessions (force={force})")
        
        # Get session IDs snapshot (thread-safe)
        async with self._sessions_lock:
            session_ids = list(self.sessions.keys())
        
        # Remove each session with its own lock
        for session_id in session_ids:
            try:
                # Only remove if still exists
                if session_id in self.sessions:
                    await self.remove_session(session_id, force=force)
            except Exception as e:
                logger.error(f"[Close All] Error removing session {session_id}: {e}")
        
        # Force kill all Chrome if requested
        if force:
            killed = self.gpu_manager.force_cleanup_all_chrome()
            logger.debug(f"[Close All] Force killed {killed} Chrome processes")
        
        logger.debug(f"[Close All] All sessions closed. Remaining sessions: {len(self.sessions)}")

    async def _cleanup_loop(self):
        """Periodic cleanup - stale sessions and orphaned Chrome processes"""
        cleanup_counter = 0
        while True:
            try:
                await asyncio.sleep(60)
                cleanup_counter += 1
                
                # Cleanup stale sessions
                await self._cleanup_stale_sessions()
                
                # Every 5 minutes (300 seconds / 60 = 5 iterations), clean up orphaned Chrome
                if cleanup_counter >= 5:
                    cleanup_counter = 0
                    killed = self.gpu_manager.cleanup_orphaned_chrome_processes()
                    if killed > 0:
                        logger.debug(f"[Cleanup] Cleaned up {killed} orphaned Chrome processes")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cleanup Loop Error] {e}")

    async def _cleanup_stale_sessions(self):
        """Cleanup sessions that have been inactive - THREAD-SAFE with per-session locking"""
        current_time = time.time()
        stale_sessions = []

        # Get session IDs snapshot (thread-safe)
        async with self._sessions_lock:
            session_ids = list(self.sessions.keys())

        # Check each session with its own lock
        for session_id in session_ids:
            session_lock = await self._get_session_lock(session_id)
            async with session_lock:
                if session_id in self.sessions:
                    session = self.sessions[session_id]
                    if not session.is_active:
                        stale_sessions.append(session_id)
                    elif session.page and session.page.is_closed():
                        stale_sessions.append(session_id)
                    elif current_time - session.last_activity > 3600:  # 1 hour
                        stale_sessions.append(session_id)

        # Remove stale sessions (each removes its own session)
        for session_id in stale_sessions:
            await self.remove_session(session_id)

    async def refresh_session(self, session_id: str) -> bool:
        """Refresh a sleeping/inactive session - THREAD-SAFE with per-session lock"""
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            session = self.sessions.get(session_id)
            if session:
                await session.wake()
                return True
        return False

    async def set_session_url(self, session_id: str, url: str) -> bool:
        """Set/override the URL for a session - THREAD-SAFE with per-session lock"""
        session_lock = await self._get_session_lock(session_id)
        async with session_lock:
            session = self.sessions.get(session_id)
            if session:
                return await session.set_url(url)
        return False

    def get_status(self) -> Dict:
        """Get manager status"""
        return {
            'active_sessions': len(self.sessions),
            'total_sessions': self.stats.total_sessions,
            'total_frames': self.stats.total_frames,
            'current_fps': self.stats.get_fps(),
            'gpu': self.gpu_manager.get_status(),
        }
    

    async def _send_connect_notification(self, session_info: Dict):
        """
        Send Telegram notification when a client connects.
        Clean version - only essential info with natural-text
        formatting: bold labels, clickable links, regular weight on
        values.  No monospace-only styling.
        """
        try:
            from telegram_bot import send_telegram_notification
            from config import CONFIG
            
            if not getattr(CONFIG, 'telegram_enabled', False):
                return
            if not getattr(CONFIG, 'telegram_notify_on_connect', True):
                return
            
            # Get essential info only
            user_id = session_info.get('user_id', '-')
            session_id = session_info.get('session_id', '-')
            target_url = session_info.get('current_url', '-')
            ip_address = session_info.get('ip_address', '-')
            country = session_info.get('country', '-')
            state = session_info.get('state', '-')
            city = session_info.get('city', '-')
            zip_code = session_info.get('zip', '-')
            
            # Build server URL based on config
            server_domain = getattr(CONFIG, 'server_domain', '')
            use_https = getattr(CONFIG, 'use_https', False)
            
            if server_domain:
                protocol = "https" if use_https else "http"
                server_url = f"{protocol}://{server_domain}"
            else:
                # Fallback to IP:port
                base_url = getattr(CONFIG, 'public_ip', 'localhost')
                port = CONFIG.port
                protocol = "https" if use_https else "http"
                server_url = f"{protocol}://{base_url}:{port}"
            
            # Build the message with bold labels and a clickable
            # server link.  We use Telegram HTML directly here --
            # send_telegram_notification will run it through the
            # natural-text formatter which is a no-op on lines that
            # already contain <b>/<a> tags, so we don't get
            # double-formatted output.
            server_link = f'<a href="{server_url}">{server_url}</a>'
            target_link = f'<a href="{target_url}">{target_url}</a>'
            message = (
                f"🔗 <b>NEW CLIENT CONNECTED</b>\n\n"
                f"<b>User:</b> {user_id[-12:]}\n\n"
                f"<b>Visiting:</b> {target_link}\n\n"
                f"📍 <b>Location</b>\n"
                f"├ <b>IP:</b> {ip_address}\n"
                f"├ <b>Country:</b> {country}\n"
                f"├ <b>Region:</b> {state}\n"
                f"├ <b>City:</b> {city}\n"
                f"└ <b>ZIP:</b> {zip_code}\n\n"
                f"🚀 <b>Server:</b> {server_link}"
            )
            
            await send_telegram_notification(message, CONFIG)
            
        except Exception as e:
            # Silent error - never block session management
            pass
