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
        self.max_sessions = getattr(config, 'max_sessions', 20)
        self.min_free_memory_mb = int(os.environ.get('MIN_FREE_MEMORY_MB', 56 if is_windows() else 512))
        self.min_free_disk_gb = 2  # Minimum free disk space required

    def get_gpu_for_session(self) -> int:
        """Return dummy GPU ID (no GPU needed)"""
        return 0

    def register_session(self, session_id: str, gpu_id: int):
        """Register a new session"""
        self.sessions[session_id] = time.time()
        logger.debug(f"Registered session {session_id}, total: {len(self.sessions)}")

    def unregister_session(self, session_id: str, gpu_id: int):
        """Unregister a session"""
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.debug(f"Unregistered session {session_id}, remaining: {len(self.sessions)}")

    def get_active_session_count(self) -> int:
        """Get count of active sessions"""
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
            'active_sessions': len(self.sessions),
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
        if len(self.sessions) >= self.max_sessions:
            logger.warning(f"Max sessions reached: {len(self.sessions)}/{self.max_sessions}")
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
        
        # Check session limit
        if len(self.sessions) >= self.max_sessions:
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
        """
        Cleanup orphaned Chrome processes that are not associated with active sessions.
        Returns number of processes killed.
        """
        killed = 0
        try:
            if is_windows():
                # Windows: Use tasklist to find chrome processes
                result = subprocess.run(
                    ['tasklist', '/FI', 'IMAGENAME eq chrome.exe', '/FO', 'CSV', '/NH'],
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    return 0
                
                for line in result.stdout.strip().split('\n'):
                    if not line or 'chrome.exe' not in line.lower():
                        continue
                    try:
                        # Parse CSV format: "chrome.exe","1234","Console","1"...
                        parts = line.split(',')
                        if len(parts) >= 2:
                            pid = int(parts[1].strip('"'))
                            proc = psutil.Process(pid)
                            
                            # Skip if process is zombie
                            if proc.status() == psutil.STATUS_ZOMBIE:
                                logger.debug(f"Killing zombie Chrome process {pid}")
                                proc.kill()
                                killed += 1
                                continue
                            
                            # Check parent process
                            try:
                                parent = proc.parent()
                                if parent is None or parent.pid in [0, 1]:
                                    logger.debug(f"Killing orphaned Chrome process {pid}")
                                    proc.kill()
                                    killed += 1
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                logger.debug(f"Killing unresponsive Chrome process {pid}")
                                proc.kill()
                                killed += 1
                    except (ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                    except Exception as e:
                        logger.debug(f"Error checking process: {e}")
            else:
                # Linux: Use pgrep
                result = subprocess.run(
                    ['pgrep', '-f', 'chrome'],
                    capture_output=True,
                    text=True
                )
                if result.returncode != 0:
                    return 0
                
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    if not pid:
                        continue
                    try:
                        # Check if process is a zombie or has no parent
                        proc = psutil.Process(int(pid))
                        
                        # Skip if process is defunct/zombie
                        if proc.status() == psutil.STATUS_ZOMBIE:
                            logger.debug(f"Killing zombie Chrome process {pid}")
                            proc.kill()
                            killed += 1
                            continue
                        
                        # Check parent process - if parent is gone or is init/system, kill it
                        try:
                            parent = proc.parent()
                            if parent is None or parent.pid in [1, 0]:
                                logger.debug(f"Killing orphaned Chrome process {pid}")
                                proc.kill()
                                killed += 1
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            logger.debug(f"Killing unresponsive Chrome process {pid}")
                            proc.kill()
                            killed += 1
                            
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                        # Process already gone
                        continue
                    except Exception as e:
                        logger.debug(f"Error checking process {pid}: {e}")
                    
        except Exception as e:
            logger.error(f"Error during Chrome cleanup: {e}")
        
        if killed > 0:
            logger.debug(f"Cleaned up {killed} orphaned Chrome processes")
        
        return killed

    def force_cleanup_all_chrome(self) -> int:
        """Force kill ALL Chrome processes. Returns count killed."""
        killed = 0
        try:
            if is_windows():
                # Windows: Use taskkill
                for pattern in ['chrome.exe', 'chromium.exe']:
                    result = subprocess.run(
                        ['taskkill', '/F', '/IM', pattern],
                        capture_output=True,
                        text=True
                    )
                    # Count processes killed
                    if result.returncode == 0:
                        # taskkill reports "SUCCESS:" in output
                        if 'SUCCESS' in result.stdout:
                            killed += 1
            else:
                # Linux: Use pkill
                for pattern in ['chrome', 'chromium', 'chrome-linux', 'headless_shell']:
                    result = subprocess.run(
                        ['pkill', '-9', '-f', pattern],
                        capture_output=True,
                        text=True
                    )
                    # pkill returns 1 if no processes found, which is fine
                    if result.returncode in [0, 1]:
                        # Count killed processes
                        count_result = subprocess.run(
                            ['pgrep', '-c', '-f', pattern],
                            capture_output=True,
                            text=True
                        )
                        if count_result.returncode == 0:
                            killed += int(count_result.stdout.strip())
                        
        except Exception as e:
            logger.error(f"Error during force cleanup: {e}")
        
        return killed
