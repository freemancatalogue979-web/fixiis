"""
Session Manager - Global session management and orchestration
V7: stable public-profile replacement with isolated admin impersonations
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
    """Race-safe registry for session metadata and explicit admin locks.

    The registry is deliberately *not* a singleton enforcer. A stable
    profile/user id is a parent identity used for Admin grouping; it is not a
    browser resource key. Multiple runtime sessions may therefore belong to
    the same profile. ``_session_locks`` is reserved for an explicit Admin
    lock operation and is never populated as a side effect of normal session
    creation.
    """

    def __init__(self):
        self._sessions: Dict[str, Dict] = {}
        self._profile_sessions: Dict[str, List[str]] = {}
        self._websocket_sessions: Dict[str, str] = {}
        self._session_locks: Dict[str, str] = {}
        self._registry_lock = asyncio.Lock()
        self._cleanup_task = None

    async def start(self):
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        task = self._cleanup_task
        self._cleanup_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def is_session_locked(self, user_id: str) -> bool:
        async with self._registry_lock:
            return user_id in self._session_locks

    async def get_locked_session_id(self, user_id: str) -> Optional[str]:
        async with self._registry_lock:
            return self._session_locks.get(user_id)

    async def lock_session(self, session_id: str, user_id: str) -> bool:
        """Set an explicit Admin lock for ``user_id``."""
        if not user_id:
            return False
        async with self._registry_lock:
            existing = self._session_locks.get(user_id)
            if existing and existing != session_id:
                return False
            self._session_locks[user_id] = session_id
            return True

    async def unlock_session(self, user_id: str) -> bool:
        async with self._registry_lock:
            return self._session_locks.pop(user_id, None) is not None

    async def get_locked_sessions(self) -> List[Dict]:
        async with self._registry_lock:
            return [
                {'user_id': user_id, 'session_id': session_id}
                for user_id, session_id in self._session_locks.items()
            ]

    async def create_session(self, session_id: str, profile_id: str, websocket_id: str,
                             metadata: Optional[Dict] = None) -> Optional[Dict]:
        """Register metadata without enforcing one session per profile."""
        async with self._registry_lock:
            locked_by = self._session_locks.get(profile_id)
            if locked_by and locked_by != session_id:
                return None
            existing = self._sessions.get(session_id)
            if existing is not None:
                return dict(existing)
            session_data = {
                'session_id': session_id,
                'profile_id': profile_id,
                'websocket_id': websocket_id,
                'created_at': time.time(),
                'last_active': time.time(),
                'is_active': True,
                'metadata': metadata or {},
            }
            self._sessions[session_id] = session_data
            self._profile_sessions.setdefault(profile_id, []).append(session_id)
            if websocket_id:
                self._websocket_sessions[websocket_id] = session_id
            return dict(session_data)

    async def reattach_session(self, session_id: str, websocket_id: str) -> bool:
        """Update the websocket generation for an existing runtime session."""
        async with self._registry_lock:
            value = self._sessions.get(session_id)
            if not value:
                return False
            old_websocket_id = value.get('websocket_id')
            if old_websocket_id:
                self._websocket_sessions.pop(old_websocket_id, None)
            value['websocket_id'] = websocket_id
            value['last_active'] = time.time()
            if websocket_id:
                self._websocket_sessions[websocket_id] = session_id
            return True

    async def get_session(self, session_id: str) -> Optional[Dict]:
        async with self._registry_lock:
            value = self._sessions.get(session_id)
            return dict(value) if value else None

    async def get_session_by_websocket(self, websocket_id: str) -> Optional[Dict]:
        async with self._registry_lock:
            session_id = self._websocket_sessions.get(websocket_id)
            value = self._sessions.get(session_id) if session_id else None
            return dict(value) if value else None

    async def get_profile_sessions(self, profile_id: str) -> List[Dict]:
        async with self._registry_lock:
            return [
                dict(self._sessions[sid])
                for sid in tuple(self._profile_sessions.get(profile_id, ()))
                if sid in self._sessions
            ]

    async def update_activity(self, session_id: str):
        async with self._registry_lock:
            value = self._sessions.get(session_id)
            if value:
                value['last_active'] = time.time()

    async def close_session(self, session_id: str) -> bool:
        """Remove a runtime session but preserve explicit Admin locks."""
        async with self._registry_lock:
            value = self._sessions.pop(session_id, None)
            if not value:
                return False
            profile_id = value.get('profile_id')
            if profile_id in self._profile_sessions:
                ids = self._profile_sessions[profile_id]
                try:
                    ids.remove(session_id)
                except ValueError:
                    pass
                if not ids:
                    self._profile_sessions.pop(profile_id, None)
            websocket_id = value.get('websocket_id')
            if websocket_id:
                self._websocket_sessions.pop(websocket_id, None)
            value['is_active'] = False
            return True

    async def close_websocket_session(self, websocket_id: str) -> bool:
        async with self._registry_lock:
            session_id = self._websocket_sessions.get(websocket_id)
        return await self.close_session(session_id) if session_id else False

    async def get_active_session_count(self, profile_id: Optional[str] = None) -> int:
        async with self._registry_lock:
            if profile_id:
                return sum(1 for sid in self._profile_sessions.get(profile_id, ())
                           if sid in self._sessions)
            return len(self._sessions)

    async def get_all_sessions(self) -> List[Dict]:
        async with self._registry_lock:
            return [dict(value) for value in self._sessions.values()]

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(120)
                await self._cleanup_dead_sessions()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[Registry cleanup error] %s", exc)

    async def _cleanup_dead_sessions(self):
        threshold = time.time() - 300
        async with self._registry_lock:
            session_ids = [
                sid for sid, value in self._sessions.items()
                if not value.get('is_active', True)
                or value.get('last_active', 0) < threshold
            ]
        if session_ids:
            await asyncio.gather(*(self.close_session(sid) for sid in session_ids),
                                 return_exceptions=True)


@dataclass
class _StreamLockSlot:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0


class StreamManager:
    """Per-session stream index with race-safe lock-slot lifetime."""

    def __init__(self):
        self._active_streams: Dict[str, str] = {}
        self._stream_locks: Dict[str, _StreamLockSlot] = {}
        self._locks_lock = asyncio.Lock()

    async def _acquire_stream_slot(self, session_id: str) -> _StreamLockSlot:
        async with self._locks_lock:
            slot = self._stream_locks.get(session_id)
            if slot is None:
                slot = _StreamLockSlot()
                self._stream_locks[session_id] = slot
            slot.references += 1
        try:
            await slot.lock.acquire()
            return slot
        except BaseException:
            await self._release_stream_slot(session_id, slot)
            raise

    async def _release_stream_slot(self, session_id: str, slot: _StreamLockSlot) -> None:
        async with self._locks_lock:
            slot.references = max(0, slot.references - 1)
            if (slot.references == 0
                    and session_id not in self._active_streams
                    and self._stream_locks.get(session_id) is slot):
                self._stream_locks.pop(session_id, None)

    async def register_stream(self, session_id: str) -> None:
        slot = await self._acquire_stream_slot(session_id)
        try:
            self._active_streams[session_id] = 'active'
        finally:
            slot.lock.release()
            await self._release_stream_slot(session_id, slot)

    async def unregister_stream(self, session_id: str) -> None:
        slot = await self._acquire_stream_slot(session_id)
        try:
            self._active_streams.pop(session_id, None)
        finally:
            slot.lock.release()
            await self._release_stream_slot(session_id, slot)

    async def get_active_stream_count(self) -> int:
        return len(self._active_streams)

    async def get_all_active_streams(self) -> List[str]:
        return list(self._active_streams.keys())


class SessionManager:
    """Session manager with stable-profile replacement and generation safety.

    Normal client connections intentionally have one active runtime per stable
    profile: a fresh tab/reopened link replaces the previous connection. Hidden
    admin/impersonation sessions remain explicitly isolated and may coexist.
    Per-session and per-profile locks keep unrelated users independent.
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
        self._session_lock_refs: Dict[str, int] = {}
        self._locks_lock = asyncio.Lock()  # Only for managing the locks dict
        self._sessions_lock = asyncio.Lock()  # Only for adding/removing sessions
        self._pending_session_ids: Set[str] = set()
        self.stats = SessionStats()
        self.cleanup_task = None
        self._reconnect_tokens: Dict[str, Dict] = {}
        self._pending_remove_tasks: Dict[str, asyncio.Task] = {}
        self._pending_remove_lock = asyncio.Lock()
        # Fresh non-hidden connections for the same stable profile intentionally
        # replace the previous runtime.  Per-profile locks make the
        # "disconnect old, then create new" handoff atomic without serializing
        # unrelated users.
        self._profile_replacement_locks: Dict[str, asyncio.Lock] = {}
        self._profile_replacement_locks_guard = asyncio.Lock()
        
        # Cgroup integration for session memory isolation
        self._session_cgroups: Dict[str, str] = {}
        self._cgroup_manager = None
        self._init_cgroup_manager()
    
    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """Get a per-session lock and hold one operation reference."""
        async with self._locks_lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            self._session_lock_refs[session_id] = self._session_lock_refs.get(session_id, 0) + 1
            return lock

    async def _release_session_lock(self, session_id: str):
        """Release one operation reference and reap an unused lock."""
        async with self._locks_lock:
            refs = max(0, self._session_lock_refs.get(session_id, 0) - 1)
            if refs:
                self._session_lock_refs[session_id] = refs
                return
            self._session_lock_refs.pop(session_id, None)
            if session_id not in self.sessions and session_id not in self._pending_session_ids:
                self._session_locks.pop(session_id, None)

    def _session_operation(self, session_id: str):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _operation():
            lock = await self._get_session_lock(session_id)
            try:
                async with lock:
                    yield
            finally:
                await self._release_session_lock(session_id)

        return _operation()

    async def _get_profile_replacement_lock(self, profile_id: str) -> asyncio.Lock:
        """Return the handoff lock for one stable profile identity."""
        key = str(profile_id or "")
        async with self._profile_replacement_locks_guard:
            lock = self._profile_replacement_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._profile_replacement_locks[key] = lock
            return lock

    async def schedule_remove_session(self, session_id: str, delay: float = 30.0,
                                      force: bool = False, expected_websocket=None,
                                      expected_generation: int = None):
        """Schedule removal owned by one websocket generation."""
        async with self._pending_remove_lock:
            if session_id in self._pending_remove_tasks:
                return
            task = asyncio.create_task(
                self._delayed_remove_session(
                    session_id, delay, force, expected_websocket, expected_generation
                )
            )
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

    async def _delayed_remove_session(self, session_id: str, delay: float,
                                      force: bool, expected_websocket=None,
                                      expected_generation: int = None):
        try:
            await asyncio.sleep(delay)
            async with self._pending_remove_lock:
                self._pending_remove_tasks.pop(session_id, None)
            await self.remove_session(
                session_id, force=force, expected_websocket=expected_websocket,
                expected_generation=expected_generation,
            )
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
        
    @staticmethod
    def _register_profile_session(user_id: str, session_id: str) -> None:
        try:
            from browser_manager import register_profile_session
            register_profile_session(user_id, session_id)
        except Exception:
            logger.debug("profile session registration failed", exc_info=True)

    @staticmethod
    def _unregister_profile_session(user_id: str, session_id: str) -> None:
        try:
            from browser_manager import unregister_profile_session
            unregister_profile_session(user_id, session_id)
        except Exception:
            logger.debug("profile session unregistration failed", exc_info=True)

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
        
        # Each removal owns only its per-session operation lock; run them in
        # parallel so one slow browser shutdown cannot stall unrelated users.
        await asyncio.gather(
            *(self.remove_session(session_id) for session_id in session_ids),
            return_exceptions=True,
        )

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
                             zip_code: str = None,
                             replace_existing: bool = False) -> Optional['NeoStreamingSession']:
        """Create a runtime session, optionally replacing the same profile's old one.

        Replacement is opt-in because admin/impersonation sessions are allowed
        to coexist.  Normal client links pass ``replace_existing=True`` so a
        fresh tab or a refresh with a new runtime id cannot leave the old
        browser consuming the profile and blocking the new connection.
        """
        profile_user_id = user_id or device_id or session_id
        if replace_existing and not is_impersonation:
            # An explicit Admin lock is a deliberate exception to public
            # replacement; do not terminate the locked runtime first.
            locked_by = await self.registry.get_locked_session_id(profile_user_id)
            if locked_by and locked_by != session_id:
                logger.info("[Session Create] parent %s is explicitly Admin-locked", profile_user_id)
                return None
            profile_lock = await self._get_profile_replacement_lock(profile_user_id)
            async with profile_lock:
                await self.kick_previous_sessions(
                    profile_user_id,
                    exclude_session_id=session_id,
                    replace_existing=True,
                )
                return await self._create_session(
                    session_id, websocket, user_agent, viewport, pixel_ratio,
                    url=url, device_id=device_id, user_id=user_id,
                    reconnect_token=reconnect_token,
                    is_impersonation=is_impersonation,
                    is_mobile=is_mobile,
                    client_ip=client_ip, country=country, state=state,
                    city=city, zip_code=zip_code,
                )
        return await self._create_session(
            session_id, websocket, user_agent, viewport, pixel_ratio,
            url=url, device_id=device_id, user_id=user_id,
            reconnect_token=reconnect_token,
            is_impersonation=is_impersonation,
            is_mobile=is_mobile,
            client_ip=client_ip, country=country, state=state,
            city=city, zip_code=zip_code,
        )

    async def _create_session(self, session_id: str, websocket, user_agent: str,
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
        """Create or reattach one explicitly identified runtime session.

        ``session_id`` owns runtime resources. ``user_id`` is the durable
        parent identity used for profile persistence and Admin grouping. The
        public wrapper performs any requested same-profile replacement before
        this method runs. Reattachment is limited to the same runtime id or an
        explicit matching reconnect token.
        """
        profile_user_id = user_id or device_id or session_id
        reservation_held = False

        async with self._session_operation(session_id):
            registry_registered = False
            manager_registered = False
            stream_registered = False
            profile_registered = False
            locked_by = await self.registry.get_locked_session_id(profile_user_id)
            if locked_by and locked_by != session_id:
                logger.info("[Session Create] parent %s is explicitly Admin-locked", profile_user_id)
                return None
            await self.cancel_scheduled_removal(session_id)

            async with self._sessions_lock:
                existing_session = self.sessions.get(session_id)
                if existing_session is not None:
                    if getattr(existing_session, 'user_id', None) != profile_user_id:
                        logger.warning(
                            "[Session Create] refusing identity collision for %s: %s != %s",
                            session_id, getattr(existing_session, 'user_id', None), profile_user_id,
                        )
                        return None
                    reattach_session = existing_session
                else:
                    reattach_session = None
                    if reconnect_token:
                        token_data = self._reconnect_tokens.get(reconnect_token)
                        if token_data and token_data.get('profile_id') == profile_user_id:
                            token_sid = token_data.get('session_id')
                            # A token may reattach only its own runtime id.
                            # Parent identity alone is never a browser ownership
                            # credential and cannot select another tab.
                            if token_sid == session_id:
                                candidate = self.sessions.get(token_sid)
                                if (candidate is not None
                                        and getattr(candidate, 'user_id', None) == profile_user_id):
                                    reattach_session = candidate

                    if reattach_session is None:
                        try:
                            if hasattr(self.gpu_manager, 'reserve_session'):
                                can_start, reason = self.gpu_manager.reserve_session(session_id, 0)
                                reservation_held = bool(can_start)
                            else:
                                can_start, reason = self.gpu_manager.check_resources_available()
                        except Exception as exc:
                            can_start, reason = False, f"resource check failed: {exc}"
                        if not can_start:
                            logger.warning("[Session Create] Cannot create %s: %s", session_id, reason)
                            return None
                        max_sessions = getattr(self.config, 'max_sessions', 20)
                        if len(self.sessions) + len(self._pending_session_ids) >= max_sessions:
                            logger.warning(
                                "[Session Create] Cannot create %s: max sessions reached (%s/%s)",
                                session_id, len(self.sessions), max_sessions,
                            )
                            return None
                        self._pending_session_ids.add(session_id)

            if reattach_session is not None:
                try:
                    generation = await reattach_session.reattach_websocket(websocket)
                    if generation is None:
                        raise RuntimeError("websocket reattachment was rejected")
                    await self.registry.reattach_session(
                        reattach_session.session_id, str(id(websocket))
                    )
                    self.stats.record_reconnect()
                    self._refresh_reconnect_token(
                        reattach_session.session_id, profile_user_id
                    )
                    return reattach_session
                except Exception:
                    logger.exception(
                        "[Session Create] reattach failed for %s",
                        reattach_session.session_id,
                    )
                    return None

            session = None

            async def _rollback_created_session() -> None:
                """Undo every partial registration if startup fails mid-flight."""
                nonlocal registry_registered, manager_registered, stream_registered, profile_registered
                if profile_registered:
                    self._unregister_profile_session(profile_user_id, session_id)
                    profile_registered = False
                if manager_registered:
                    async with self._sessions_lock:
                        if self.sessions.get(session_id) is session:
                            self.sessions.pop(session_id, None)
                            self.stats.active_sessions = max(
                                0, self.stats.active_sessions - 1
                            )
                    manager_registered = False
                if stream_registered:
                    try:
                        await self.stream_manager.unregister_stream(session_id)
                    except Exception:
                        logger.debug(
                            "[Session Create] rollback stream unregister failed",
                            exc_info=True,
                        )
                    stream_registered = False
                if registry_registered:
                    try:
                        await self.registry.close_session(session_id)
                    except Exception:
                        logger.debug(
                            "[Session Create] rollback registry close failed",
                            exc_info=True,
                        )
                    registry_registered = False
                self._invalidate_reconnect_token(session_id)
                try:
                    self._cleanup_session_cgroup(session_id)
                except Exception:
                    pass
                if session is not None:
                    try:
                        await session.cleanup(force=True)
                    except Exception:
                        logger.debug(
                            "[Session Create] rollback browser cleanup failed",
                            exc_info=True,
                        )

            try:
                from session import NeoStreamingSession
                logger.debug(
                    "[Session Create] creating runtime session %s for parent %s",
                    session_id, profile_user_id,
                )
                session_lock = self._session_locks.get(session_id)
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
                    zip_code=zip_code,
                )
                session.device_id = device_id or session_id
                session.user_id = profile_user_id
                session.is_impersonation = bool(is_impersonation)
                session._session_lock = session_lock
                session._session_registry = self.registry
                session.client_ip = client_ip or '-'
                session.country = country or '-'
                session.state = state or '-'
                session.city = city or '-'
                session.zip_code = zip_code or '-'
                session.client_user_agent = user_agent

                success = await session.start(url)
                if not success:
                    logger.error("[Session Create] session %s start() returned False", session_id)
                    try:
                        await session.cleanup(force=True)
                    except Exception:
                        logger.debug("[Session Create] failed-session cleanup failed", exc_info=True)
                    return None

                registry_entry = await self.registry.create_session(
                    session_id, profile_user_id, str(id(websocket)),
                    metadata={'device_id': device_id or session_id, 'is_hidden': is_impersonation},
                )
                if registry_entry is None:
                    logger.info("[Session Create] %s became locked while starting", session_id)
                    await _rollback_created_session()
                    return None

                registry_registered = True
                async with self._sessions_lock:
                    self._pending_session_ids.discard(session_id)
                    self.sessions[session_id] = session
                    self.stats.active_sessions += 1
                    self.stats.total_sessions += 1
                    manager_registered = True

                self.gpu_manager.register_session(session_id, 0)
                reservation_held = False
                await self.stream_manager.register_stream(session_id)
                stream_registered = True
                self._create_session_cgroup(session_id)
                self._generate_reconnect_token(session_id, profile_user_id)
                self._register_profile_session(profile_user_id, session_id)
                profile_registered = True

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
                    'user_agent': session.client_user_agent,
                }))
                logger.debug(
                    "[Session Create] session %s created successfully. Active: %s/%s",
                    session_id, len(self.sessions), getattr(self.config, 'max_sessions', 20),
                )
                return session
            except asyncio.CancelledError:
                await _rollback_created_session()
                raise
            except MemoryError:
                logger.exception("[Session Create] out of memory creating %s", session_id)
                await _rollback_created_session()
                return None
            except Exception:
                logger.exception("[Session Create] failed creating %s", session_id)
                await _rollback_created_session()
                return None
            finally:
                if reservation_held and hasattr(self.gpu_manager, 'release_session_reservation'):
                    try:
                        self.gpu_manager.release_session_reservation(session_id)
                    except Exception:
                        logger.debug("[Session Create] reservation release failed", exc_info=True)
                async with self._sessions_lock:
                    self._pending_session_ids.discard(session_id)

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
        """Return a live session without creating a lock for an unknown ID."""
        async with self._sessions_lock:
            return self.sessions.get(session_id)

    async def get_session_by_user(self, user_id: str) -> List['NeoStreamingSession']:
        """Snapshot all runtime sessions belonging to one parent identity."""
        async with self._sessions_lock:
            return [
                session for session in tuple(self.sessions.values())
                if getattr(session, 'user_id', None) == user_id
            ]

    async def get_session_by_tag(self, client_tag: str) -> List['NeoStreamingSession']:
        """Lookup by an explicit parent/device tag without mutating state."""
        if not client_tag:
            return []
        async with self._sessions_lock:
            return [
                session for session in tuple(self.sessions.values())
                if getattr(session, 'user_id', None) == client_tag
                or getattr(session, 'device_id', None) == client_tag
            ]

    async def kick_previous_sessions(self, client_tag: str, exclude_session_id: str = None,
                                     *, replace_existing: bool = True) -> int:
        """Explicitly replace sessions for one parent identity.

        The method is also used by the normal connection handoff. Runtime
        resource ownership remains keyed by explicit session ids, while this
        stable-profile operation makes the replacement intentional and
        generation-safe.
        """
        if not client_tag or not replace_existing:
            return 0
        async with self._sessions_lock:
            candidates = [
                sid for sid, session in self.sessions.items()
                if sid != exclude_session_id
                and getattr(session, 'user_id', None) == client_tag
                and not getattr(session, 'is_impersonation', False)
            ]
        kicked = 0
        for old_sid in candidates:
            try:
                async with self._sessions_lock:
                    previous = self.sessions.get(old_sid)
                    previous_ws = getattr(previous, 'websocket', None) if previous else None
                if await self.remove_session(old_sid, force=True):
                    kicked += 1
                if previous_ws is not None:
                    # Complete the old websocket handoff before returning so
                    # the replacement can start without a live predecessor.
                    await self._safe_close_ws(previous_ws)
            except Exception:
                logger.warning("[Kick] failed to replace runtime session %s", old_sid,
                               exc_info=True)
        return kicked

    @staticmethod
    async def _safe_close_ws(ws):
        try:
            await ws.close(code=1000, reason="replaced_by_new_session")
        except Exception:
            pass

    async def remove_session(self, session_id: str, force: bool = False,
                             expected_websocket=None, expected_generation: int = None):
        """Remove one runtime session without holding a global lock over cleanup."""
        async with self._session_operation(session_id):
            await self.cancel_scheduled_removal(session_id)
            async with self._sessions_lock:
                session = self.sessions.get(session_id)
                if session is None:
                    return False
                if expected_websocket is not None:
                    checker = getattr(session, "is_websocket_current", None)
                    current = (
                        checker(expected_websocket, expected_generation)
                        if callable(checker)
                        else getattr(session, 'websocket', None) is expected_websocket
                    )
                    if not current:
                        # A stale disconnect cannot remove a newer websocket owner.
                        return False
                self.sessions.pop(session_id, None)
                self._pending_session_ids.discard(session_id)
                self.stats.active_sessions = max(0, self.stats.active_sessions - 1)
                profile_user_id = getattr(session, "user_id", None)

            self._unregister_profile_session(profile_user_id, session_id)
            logger.debug("[Session Remove] removing %s (force=%s)", session_id, force)
            try:
                await self.stream_manager.unregister_stream(session_id)
            except Exception:
                logger.warning("[Session Remove] stream unregister failed for %s",
                               session_id, exc_info=True)
            try:
                await self.registry.close_session(session_id)
            except Exception:
                logger.warning("[Session Remove] registry close failed for %s",
                               session_id, exc_info=True)
            self._invalidate_reconnect_token(session_id)
            try:
                self.gpu_manager.unregister_session(session_id, 0)
            except Exception:
                logger.warning("[Session Remove] GPU unregister failed for %s",
                               session_id, exc_info=True)
            try:
                await session.cleanup(force=force)
            except Exception:
                logger.warning("[Session Remove] browser cleanup failed for %s",
                               session_id, exc_info=True)
            try:
                self._cleanup_session_cgroup(session_id)
            except Exception:
                logger.warning("[Session Remove] cgroup cleanup failed for %s",
                               session_id, exc_info=True)
            logger.debug("[Session Remove] removed %s; active=%s",
                         session_id, self.stats.active_sessions)
            return True

    async def pause_session(self, user_id: str) -> bool:
        """Pause all sessions for a user"""
        sessions = await self.get_session_by_user(user_id)
        if not sessions:
            return False
        await asyncio.gather(*(session.enter_sleep() for session in sessions), return_exceptions=True)
        self.stats.paused_sessions += len(sessions)
        return True

    async def resume_session(self, user_id: str) -> bool:
        """Resume all paused sessions for a user"""
        sessions = await self.get_session_by_user(user_id)
        if not sessions:
            return False
        await asyncio.gather(*(session.wake() for session in sessions), return_exceptions=True)
        self.stats.paused_sessions = max(0, self.stats.paused_sessions - len(sessions))
        return True

    async def shutdown_session(self, user_id: str) -> bool:
        """Shutdown all sessions for a user - THREAD-SAFE with per-session locking
        
        CRITICAL FIX: Each session is removed independently without blocking others
        """
        sessions = await self.get_session_by_user(user_id)
        if not sessions:
            return False
        results = await asyncio.gather(
            *(self.remove_session(session.session_id, force=True) for session in sessions),
            return_exceptions=True,
        )
        return any(result is True for result in results)

    async def get_all_sessions(self) -> List[Dict]:
        """Get a concurrent, race-safe snapshot of active session info."""
        async with self._sessions_lock:
            sessions = tuple(self.sessions.values())

        async def _info(session):
            try:
                return await session.get_info()
            except Exception as exc:
                logger.warning(
                    "[Get All] Error getting info for session %s: %s",
                    getattr(session, "session_id", "?"), exc,
                )
                return None

        results = await asyncio.gather(*(_info(session) for session in sessions))
        return [item for item in results if isinstance(item, dict)]

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
        
        # Remove each session with its own lock.  A server-wide Chrome kill is
        # deliberately not used: it can terminate a browser belonging to an
        # unrelated session (or an operator's own browser).  ``force`` is
        # passed to each session's targeted cleanup instead.
        results = await asyncio.gather(
            *(self.remove_session(session_id, force=force) for session_id in session_ids),
            return_exceptions=True,
        )
        for session_id, result in zip(session_ids, results):
            if isinstance(result, Exception):
                logger.error("[Close All] Error removing session %s: %s", session_id, result)

        async with self._sessions_lock:
            remaining = len(self.sessions)
        logger.debug(f"[Close All] All sessions closed. Remaining sessions: {remaining}")

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
                    killed = await asyncio.to_thread(
                        self.gpu_manager.cleanup_orphaned_chrome_processes
                    )
                    if killed > 0:
                        logger.debug(f"[Cleanup] Cleaned up {killed} orphaned Chrome processes")
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Cleanup Loop Error] {e}")

    async def _cleanup_stale_sessions(self):
        """Find stale sessions from a snapshot, then remove them safely."""
        current_time = time.time()
        async with self._sessions_lock:
            snapshot = list(self.sessions.items())
        stale_sessions = []
        for session_id, session in snapshot:
            try:
                page = getattr(session, 'page', None)
                page_closed = bool(page and page.is_closed())
            except Exception:
                page_closed = False
            if (not getattr(session, 'is_active', True)
                    or page_closed
                    or current_time - getattr(session, 'last_activity', current_time) > 3600):
                stale_sessions.append((
                    session_id,
                    getattr(session, "websocket", None),
                    getattr(session, "websocket_generation", None),
                ))
        await asyncio.gather(
            *(self.remove_session(
                session_id, expected_websocket=expected_websocket,
                expected_generation=expected_generation,
            ) for session_id, expected_websocket, expected_generation in stale_sessions),
            return_exceptions=True,
        )

    async def refresh_session(self, session_id: str) -> bool:
        """Wake one session while serializing only that session's operation."""
        async with self._session_operation(session_id):
            async with self._sessions_lock:
                session = self.sessions.get(session_id)
            if session:
                await session.wake()
                return True
        return False

    async def set_session_url(self, session_id: str, url: str) -> bool:
        """Set a URL for one session without creating a lock for bad IDs."""
        async with self._session_operation(session_id):
            async with self._sessions_lock:
                session = self.sessions.get(session_id)
            if session:
                return await session.set_url(url)
        return False

    def get_status(self) -> Dict:
        """Get manager status"""
        return {
            'active_sessions': self.stats.active_sessions,
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
