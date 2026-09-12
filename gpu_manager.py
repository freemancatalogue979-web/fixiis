"""
GPU Manager - Enhanced Resource Management
Handles session limits, system resource monitoring, and cleanup
GPU encoding removed - uses pure CDP screencast
"""

import os
import psutil
import subprocess
import time
import logging
import threading
import platform
from typing import Tuple, Dict, Optional


def is_windows() -> bool:
    """Check if running on Windows"""
    return platform.system().lower() == 'windows'

def is_linux() -> bool:
    """Check if running on Linux"""
    return platform.system().lower() == 'linux'

logger = logging.getLogger(__name__)


class GPUManager:
    """Enhanced GPU Manager with resource monitoring and session limits"""

    def __init__(self, config):
        self.config = config
        self.sessions: Dict[str, int] = {}  # session_id -> start_time
        self._reserved_sessions: Dict[str, float] = {}
        # Runtime profile ownership lets orphan cleanup distinguish this
        # process's browser trees from unrelated Chrome installations.
        self._runtime_profiles: Dict[str, str] = {}
        self._sessions_lock = threading.RLock()
        self.max_sessions = getattr(config, 'max_sessions', 20)
        self.min_free_memory_mb = int(os.environ.get('MIN_FREE_MEMORY_MB', 56 if is_windows() else 512))
        self.min_free_disk_gb = 2  # Minimum free disk space required

    def get_gpu_for_session(self) -> int:
        """Return dummy GPU ID (no GPU needed)"""
        return 0

    def reserve_session(self, session_id: str, gpu_id: int = 0) -> Tuple[bool, str]:
        """Atomically reserve capacity before slow browser startup."""
        with self._sessions_lock:
            if session_id in self.sessions:
                return True, "already active"
            if session_id in self._reserved_sessions:
                return True, "already reserved"
            if len(self.sessions) + len(self._reserved_sessions) >= self.max_sessions:
                return False, f"Max sessions reached ({self.max_sessions})"
            memory_info = self._get_memory_info()
            if memory_info['available_mb'] < self.min_free_memory_mb:
                return False, (
                    f"Low memory ({memory_info['available_mb']:.0f}MB available, "
                    f"need {self.min_free_memory_mb}MB)"
                )
            disk_info = self._get_disk_info()
            if disk_info['free_gb'] < self.min_free_disk_gb:
                return False, f"Low disk space ({disk_info['free_gb']:.1f}GB free)"
            self._reserved_sessions[session_id] = time.time()
            logger.debug("Reserved resources for session %s (active=%s reserved=%s)",
                         session_id, len(self.sessions), len(self._reserved_sessions))
            return True, "Resources reserved"

    def release_session_reservation(self, session_id: str) -> bool:
        with self._sessions_lock:
            return self._reserved_sessions.pop(session_id, None) is not None

    def register_session(self, session_id: str, gpu_id: int):
        """Commit a reservation as an active session; idempotent."""
        with self._sessions_lock:
            self._reserved_sessions.pop(session_id, None)
            self.sessions[session_id] = time.time()
            logger.debug(f"Registered session {session_id}, total: {len(self.sessions)}")

    def unregister_session(self, session_id: str, gpu_id: int = 0):
        """Release active state and any outstanding reservation exactly once."""
        with self._sessions_lock:
            was_active = self.sessions.pop(session_id, None) is not None
            was_reserved = self._reserved_sessions.pop(session_id, None) is not None
            if was_active or was_reserved:
                logger.debug(f"Unregistered session {session_id}, remaining: {len(self.sessions)}")
            return was_active or was_reserved

    def register_runtime_profile(self, session_id: str, profile_dir: str) -> None:
        if not session_id or not profile_dir:
            return
        with self._sessions_lock:
            self._runtime_profiles[str(session_id)] = os.path.realpath(str(profile_dir))

    def unregister_runtime_profile(self, session_id: str) -> None:
        with self._sessions_lock:
            self._runtime_profiles.pop(str(session_id), None)

    def get_active_runtime_profiles(self) -> set:
        with self._sessions_lock:
            return set(self._runtime_profiles.values())

    def get_active_session_count(self) -> int:
        """Get count of active sessions"""
        with self._sessions_lock:
            return len(self.sessions)

    def get_status(self) -> dict:
        """Get comprehensive system status"""
        memory_info = self._get_memory_info()
        disk_info = self._get_disk_info()
        
        return {
            'available': True,
            'type': 'none',
            'count': 0,
            'encoder_type': 'png_screencast',
            'active_sessions': self.get_active_session_count(),
            'max_sessions': self.max_sessions,
            'memory_available_mb': memory_info['available_mb'],
            'memory_used_percent': memory_info['used_percent'],
            'disk_free_gb': disk_info['free_gb'],
            'system_healthy': self._is_system_healthy()
        }

    def get_encoder_type(self) -> str:
        return "png_screencast"

    def _get_memory_info(self) -> Dict:
        """Get system memory information"""
        try:
            mem = psutil.virtual_memory()
            return {
                'total_mb': mem.total / (1024 * 1024),
                'available_mb': mem.available / (1024 * 1024),
                'free_mb': mem.free / (1024 * 1024),
                'used_percent': mem.percent,
                'swap_total_mb': getattr(mem, 'swapped', 0) / (1024 * 1024)
            }
        except Exception as e:
            logger.warning(f"Failed to get memory info: {e}")
            return {'total_mb': 0, 'available_mb': 0, 'free_mb': 0, 'used_percent': 0}

    def _get_disk_info(self) -> Dict:
        """Get disk space information"""
        try:
            disk = psutil.disk_usage('/')
            return {
                'total_gb': disk.total / (1024 ** 3),
                'free_gb': disk.free / (1024 ** 3),
                'used_percent': disk.percent
            }
        except Exception as e:
            logger.warning(f"Failed to get disk info: {e}")
            return {'total_gb': 0, 'free_gb': 0, 'used_percent': 0}

    def _get_chrome_process_count(self) -> int:
        """Count running Chrome processes"""
        try:
            if is_windows():
                # Windows: Use tasklist
                result = subprocess.run(
                    ['tasklist', '/FI', 'IMAGENAME eq chrome.exe'],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    # Count lines that contain chrome.exe
                    lines = result.stdout.split('\n')
                    count = sum(1 for line in lines if 'chrome.exe' in line.lower())
                    return count
            else:
                # Linux: Use pgrep
                result = subprocess.run(
                    ['pgrep', '-c', '-f', 'chrome'],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    return int(result.stdout.strip())
        except Exception:
            pass
        return 0

    def _is_system_healthy(self) -> bool:
        """Check if system is healthy enough to create new sessions"""
        memory_info = self._get_memory_info()
        disk_info = self._get_disk_info()
        
        # Check memory
        if memory_info['available_mb'] < self.min_free_memory_mb:
            # Polled continuously — a per-tick WARNING turned into console
            # spam; low-memory is a state, not an event. DEBUG keeps it
            # reachable via LOG_LEVEL=DEBUG without flooding production.
            logger.debug(
                f"Low memory: {memory_info['available_mb']:.1f}MB available, "
                f"{memory_info['free_mb']:.1f}MB free"
            )
            return False
        
        # Check disk space
        if disk_info['free_gb'] < self.min_free_disk_gb:
            logger.warning(f"Low disk space: {disk_info['free_gb']:.1f}GB free")
            return False
        
        # Check session count
        with self._sessions_lock:
            _session_total = len(self.sessions) + len(self._reserved_sessions)
        if _session_total >= self.max_sessions:
            logger.warning(f"Max sessions reached: {_session_total}/{self.max_sessions}")
            return False
        
        return True

    def check_resources_available(self) -> Tuple[bool, str]:
        """
        Check if resources are available to create a new session.
        Returns (can_start, reason)
        """
        memory_info = self._get_memory_info()
        disk_info = self._get_disk_info()
        chrome_count = self._get_chrome_process_count()
        
        # Check session limit including startup reservations.
        with self._sessions_lock:
            _session_total = len(self.sessions) + len(self._reserved_sessions)
        if _session_total >= self.max_sessions:
            return False, f"Max sessions reached ({self.max_sessions})"
        
        # Check memory using available memory instead of free memory for Windows compatibility
        if memory_info['available_mb'] < self.min_free_memory_mb:
            return False, (
                f"Low memory ({memory_info['available_mb']:.0f}MB available, "
                f"{memory_info['free_mb']:.0f}MB free, need {self.min_free_memory_mb}MB)"
            )
        
        # Check disk space
        if disk_info['free_gb'] < self.min_free_disk_gb:
            return False, f"Low disk space ({disk_info['free_gb']:.1f}GB free)"
        
        return True, "Resources available"

    def cleanup_orphaned_chrome_processes(self) -> int:
        """Kill only stale browsers in this app's private runtime tree.

        A process named Chrome is not evidence that this service owns it.  The
        old implementation scanned every Chrome process and killed anything
        whose parent looked orphaned, which could terminate a user's personal
        browser or another tenant's session.  Runtime browsers use
        ``<profile_base_path>/.runtime_sessions/...``; exact profile matching
        plus the in-process ownership registry keeps cleanup scoped.
        """
        if not is_linux() and not is_windows():
            return 0
        try:
            root = (
                os.path.realpath(
                    os.path.join(
                        str(getattr(self.config, 'profile_base_path', '')),
                        '.runtime_sessions',
                    )
                )
                if getattr(self.config, 'profile_base_path', None)
                else ''
            )
            if not root or not os.path.isdir(root):
                return 0
            active_profiles = self.get_active_runtime_profiles()
            killed = 0

            def profile_arg(args):
                for index, arg in enumerate(args):
                    if arg == '--user-data-dir' and index + 1 < len(args):
                        return args[index + 1]
                    if arg.startswith('--user-data-dir='):
                        return arg.split('=', 1)[1]
                return None

            for proc in psutil.process_iter(['pid', 'cmdline']):
                try:
                    args = [str(part) for part in (proc.info.get('cmdline') or [])]
                    profile = profile_arg(args)
                    if not profile:
                        continue
                    profile = os.path.realpath(profile)
                    # Only descendants of our private runtime root are
                    # eligible.  Stable user profiles and personal Chrome are
                    # never touched.
                    if not (profile == root or profile.startswith(root + os.sep)):
                        continue
                    if profile in active_profiles:
                        continue
                    proc.kill()
                    killed += 1
                    logger.debug("Killed stale owned Chrome PID %s (profile=%s)", proc.pid, profile)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                except Exception:
                    logger.debug("Error checking runtime browser process", exc_info=True)
            return killed
        except Exception as e:
            logger.error("Error during scoped Chrome cleanup: %s", e)
            return 0

    def force_cleanup_all_chrome(self) -> int:
        """Deprecated compatibility hook; never kill global Chrome processes.

        Cleanup is intentionally restricted to exact private runtime profiles
        owned by this service instance.
        """
        logger.warning(
            "Ignoring global Chrome cleanup request; global process termination is disabled"
        )
        return 0
