"""
Memory Manager Module - Handles system memory management and cgroup isolation
Linux-specific functionality is disabled on Windows
"""

import os
import subprocess
import logging
import time
import signal
import platform
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    """Check if running on Windows"""
    return platform.system().lower() == 'windows'


def is_linux() -> bool:
    """Check if running on Linux"""
    return platform.system().lower() == 'linux'


# ==============================
# Memory Management Functions
# ==============================

def setup_swap_space(swap_size_gb: int = 64):
    """
    Setup swap space for handling memory overflow situations.
    This prevents OOM kills when sessions consume too much memory.
    LINUX ONLY - Skip on Windows.
    
    Args:
        swap_size_gb: Size of swap file in GB (default: 64GB)
    """
    # Skip swap setup on Windows
    if is_windows():
        logger.debug("Swap space setup skipped on Windows")
        return True
    
    swap_path = "/swapfile"
    swap_size_bytes = swap_size_gb * 1024 * 1024 * 1024
    
    try:
        # Check if swap is already configured
        result = subprocess.run(['swapon', '--show'], capture_output=True, text=True)
        if result.returncode == 0 and swap_path in result.stdout:
            logger.debug(f"Swap space already configured at {swap_path}")
            return True
        
        # Check current swap configuration
        result = subprocess.run(['free', '-h'], capture_output=True, text=True)
        logger.debug(f"Current memory status:\n{result.stdout}")
        
        # Disable existing swap
        logger.debug("Disabling existing swap...")
        subprocess.run(['swapoff', '-a'], capture_output=True)
        time.sleep(1)
        
        # Remove old swapfile if exists
        if os.path.exists(swap_path):
            os.remove(swap_path)
            logger.debug(f"Removed old swapfile")
        
        # Create new swap file
        logger.debug(f"Creating {swap_size_gb}GB swap file...")
        
        # Use fallocate for fast creation
        result = subprocess.run(
            ['fallocate', '-l', f'{swap_size_gb}G', swap_path],
            capture_output=True
        )
        
        if result.returncode != 0:
            # Fallback to dd if fallocate fails
            logger.warning("fallocate failed, using dd...")
            subprocess.run(
                ['dd', 'if=/dev/zero', f'of={swap_path}', 
                 'bs=1M', f'count={swap_size_gb * 1024}'],
                capture_output=True
            )
        
        # Set permissions
        os.chmod(swap_path, 0o600)
        
        # Make swap
        subprocess.run(['mkswap', swap_path], capture_output=True)
        
        # Enable swap
        result = subprocess.run(['swapon', swap_path], capture_output=True)
        
        if result.returncode == 0:
            logger.debug(f"Successfully configured {swap_size_gb}GB swap space")
            
            # Verify swap
            result = subprocess.run(['free', '-h'], capture_output=True, text=True)
            logger.debug(f"Updated memory status:\n{result.stdout}")
            return True
        else:
            logger.error(f"Failed to enable swap: {result.stderr.decode()}")
            return False
            
    except Exception as e:
        logger.error(f"Error setting up swap space: {e}")
        return False


def tune_swappiness(swappiness: int = 10):
    """
    Tune kernel swappiness to prefer swap over OOM kills.
    Lower values prevent premature swapping of application memory.
    LINUX ONLY - Skip on Windows.
    
    Args:
        swappiness: Swappiness value (0-100, default: 10)
    """
    # Skip on Windows
    if is_windows():
        logger.debug("Swappiness tuning skipped on Windows")
        return
    
    try:
        vm_path = '/proc/sys/vm/swappiness'
        if os.path.exists(vm_path):
            with open(vm_path, 'w') as f:
                f.write(str(swappiness))
            logger.debug(f"Set swappiness to {swappiness}")
        
        # Also set other memory tuning parameters
        sysctl_params = {
            '/proc/sys/vm/dirty_ratio': '60',
            '/proc/sys/vm/dirty_background_ratio': '10',
            '/proc/sys/vm/vfs_cache_pressure': '50',
            '/proc/sys/net/core/netdev_max_backlog': '5000',
            '/proc/sys/net/core/netdev_budget': '600',
        }
        
        for param_path, value in sysctl_params.items():
            if os.path.exists(param_path):
                try:
                    with open(param_path, 'w') as f:
                        f.write(value)
                except Exception:
                    pass
                    
    except Exception as e:
        logger.warning(f"Could not tune swappiness: {e}")


def setup_memory_limits():
    """
    Setup system memory limits and protections.
    Configures OOM killer behavior and memory reserves.
    LINUX ONLY - Skip on Windows.
    """
    # Skip on Windows
    if is_windows():
        logger.debug("Memory limits setup skipped on Windows")
        return
    
    try:
        # Reserve some memory for the kernel
        oom_score_adj_path = '/proc/self/oom_score_adj'
        if os.path.exists(oom_score_adj_path):
            with open(oom_score_adj_path, 'w') as f:
                f.write('-500')
            logger.debug("Configured OOM score adjustment")
        
        # Set memory reserve for critical operations
        sysctl_params = {
            '/proc/sys/vm/min_free_kbytes': '65536',
            '/proc/sys/kernel/panic': '60',
        }
        
        for param_path, value in sysctl_params.items():
            if os.path.exists(param_path):
                try:
                    with open(param_path, 'w') as f:
                        f.write(value)
                except Exception:
                    pass
                    
    except Exception as e:
        logger.warning(f"Could not setup memory limits: {e}")


def check_and_setup_memory():
    """
    Check system memory and setup swap if needed.
    Call this at server startup to ensure adequate memory resources.
    LINUX ONLY - On Windows, just log memory status.
    """
    try:
        # On Windows, use psutil for memory info
        if is_windows():
            try:
                import psutil
                mem = psutil.virtual_memory()
                total_mem_gb = mem.total / (1024**3)
                logger.debug(f"System has {total_mem_gb:.1f}GB RAM")
                logger.debug(f"Memory: {mem.percent}% used, {mem.available / (1024**3):.1f}GB available")
                return True
            except Exception as e:
                logger.warning(f"Could not get memory info on Windows: {e}")
                return False
        
        # Linux memory check using free command
        result = subprocess.run(['free', '-b'], capture_output=True, text=True)
        lines = result.stdout.strip().split('\n')
        
        if len(lines) >= 2:
            mem_line = lines[1].split()
            total_mem = int(mem_line[1])
            total_mem_gb = total_mem / (1024**3)
            
            logger.debug(f"System has {total_mem_gb:.1f}GB RAM")
            
            # Setup swap if memory is less than 32GB
            if total_mem_gb < 32:
                logger.debug("Memory < 32GB, configuring swap space...")
                setup_swap_space(swap_size_gb=64)
                tune_swappiness(swappiness=10)
            elif total_mem_gb < 64:
                logger.debug("Memory < 64GB, configuring additional swap...")
                setup_swap_space(swap_size_gb=32)
                tune_swappiness(swappiness=20)
            else:
                logger.debug("Sufficient memory available, minimal swap setup...")
                setup_swap_space(swap_size_gb=16)
                tune_swappiness(swappiness=30)
            
            # Setup memory limits
            setup_memory_limits()
            
            return True
            
    except Exception as e:
        logger.error(f"Error checking memory: {e}")
        return False


# ==============================
# Cgroup Isolation Manager
# LINUX ONLY - Disabled on Windows
# ==============================

class CgroupManager:
    """
    Manages cgroup-based resource isolation for browser sessions.
    Provides memory limits, CPU scheduling, and I/O throttling per session.
    LINUX ONLY - Disabled on Windows.
    """
    
    def __init__(self, base_cgroup: str = "/sys/fs/cgroup/neo_sessions"):
        self.base_cgroup = base_cgroup
        self.session_cgroups: Dict[str, str] = {}
        self._initialized = False
        
        # Check platform
        if is_windows():
            self.base_cgroup = None
            self.session_cgroups = {}
            self._initialized = False
            self.cgroup_version = 0
            logger.debug("Cgroup manager disabled on Windows")
            return
        
        self._check_cgroup_support()
    
    def _check_cgroup_support(self):
        """Check if cgroup v2 is available"""
        try:
            # Check for cgroup v2
            if os.path.exists('/sys/fs/cgroup/cgroup.controllers'):
                self.cgroup_version = 2
                logger.debug("Using cgroup v2")
            elif os.path.exists('/sys/fs/cgroup/cpuset'):
                self.cgroup_version = 1
                logger.debug("Using cgroup v1")
            else:
                self.cgroup_version = 0
                logger.warning("No cgroup support detected")
        except Exception:
            self.cgroup_version = 0
    
    def initialize(self):
        """Initialize the cgroup hierarchy for session isolation"""
        # Skip on Windows or if cgroup not available
        if is_windows() or self._initialized or self.cgroup_version == 0:
            return
        
        try:
            # Create base cgroup directory
            os.makedirs(self.base_cgroup, exist_ok=True)
            
            # Set default limits for the base cgroup
            self._set_base_limits()
            
            self._initialized = True
            logger.debug(f"Cgroup manager initialized at {self.base_cgroup}")
            
        except Exception as e:
            logger.warning(f"Could not initialize cgroup manager: {e}")
            self._initialized = False
    
    def _set_base_limits(self):
        """Set base resource limits for the cgroup hierarchy"""
        try:
            if self.cgroup_version == 2:
                # cgroup v2 controls
                limits = {
                    'memory.max': '32G',
                    'cpu.max': '100000 100000',
                    'io.max': '1048576',
                }
            else:
                # cgroup v1 controls
                limits = {
                    'memory.limit_in_bytes': '34359738368',
                    'cpu.cfs_quota_us': '100000',
                    'cpu.cfs_period_us': '100000',
                }
            
            for control_file, value in limits.items():
                path = os.path.join(self.base_cgroup, control_file)
                if os.path.exists(os.path.dirname(path)):
                    try:
                        with open(path, 'w') as f:
                            f.write(value)
                    except Exception:
                        pass
                        
        except Exception as e:
            logger.warning(f"Could not set base cgroup limits: {e}")
    
    def create_session_cgroup(self, session_id: str, memory_limit_mb: int = 4096) -> Optional[str]:
        """
        Create a cgroup for a specific session with memory limits.
        
        Args:
            session_id: Unique session identifier
            memory_limit_mb: Memory limit in MB (default: 4GB)
        
        Returns:
            Cgroup path if successful, None otherwise
        """
        if not self._initialized or self.cgroup_version == 0 or is_windows():
            return None
        
        cgroup_path = os.path.join(self.base_cgroup, session_id)
        
        try:
            # Create session cgroup directory
            os.makedirs(cgroup_path, exist_ok=True)
            
            # Set memory limit
            memory_limit_bytes = memory_limit_mb * 1024 * 1024
            
            if self.cgroup_version == 2:
                memory_file = os.path.join(cgroup_path, 'memory.max')
                if os.path.exists(memory_file):
                    with open(memory_file, 'w') as f:
                        f.write(str(memory_limit_bytes))
                
                cpu_file = os.path.join(cgroup_path, 'cpu.weight')
                if os.path.exists(cpu_file):
                    with open(cpu_file, 'w') as f:
                        f.write('256')
                
            else:
                memory_file = os.path.join(cgroup_path, 'memory.limit_in_bytes')
                if os.path.exists(memory_file):
                    with open(memory_file, 'w') as f:
                        f.write(str(memory_limit_bytes))
                
                cpu_file = os.path.join(cgroup_path, 'cpu.shares')
                if os.path.exists(cpu_file):
                    with open(cpu_file, 'w') as f:
                        f.write('1024')
            
            self.session_cgroups[session_id] = cgroup_path
            logger.debug(f"Created cgroup for session {session_id} with {memory_limit_mb}MB limit")
            return cgroup_path
            
        except Exception as e:
            logger.warning(f"Could not create cgroup for session {session_id}: {e}")
            return None
    
    def add_process_to_cgroup(self, session_id: str, pid: int) -> bool:
        """
        Add a process to the session's cgroup.
        
        Args:
            session_id: Session identifier
            pid: Process ID to add
        
        Returns:
            True if successful
        """
        if session_id not in self.session_cgroups or is_windows():
            return False
        
        cgroup_path = self.session_cgroups[session_id]
        
        try:
            if self.cgroup_version == 2:
                tasks_file = os.path.join(cgroup_path, 'cgroup.procs')
            else:
                tasks_file = os.path.join(cgroup_path, 'tasks')
            
            if os.path.exists(tasks_file):
                with open(tasks_file, 'w') as f:
                    f.write(str(pid))
                return True
                
        except Exception as e:
            logger.warning(f"Could not add process {pid} to cgroup: {e}")
        
        return False
    
    def remove_session_cgroup(self, session_id: str) -> bool:
        """
        Remove a session's cgroup.
        
        Args:
            session_id: Session identifier
        
        Returns:
            True if successful
        """
        if session_id not in self.session_cgroups:
            return True
        
        cgroup_path = self.session_cgroups.pop(session_id)
        
        try:
            # Kill any remaining processes in the cgroup
            if os.path.exists(cgroup_path):
                tasks_file = os.path.join(cgroup_path, 'cgroup.procs')
                if not os.path.exists(tasks_file):
                    tasks_file = os.path.join(cgroup_path, 'tasks')
                
                if os.path.exists(tasks_file):
                    with open(tasks_file, 'r') as f:
                        for line in f:
                            try:
                                pid = int(line.strip())
                                os.kill(pid, signal.SIGKILL)
                            except (ValueError, ProcessLookupError):
                                pass
                
                # Remove the cgroup directory
                os.rmdir(cgroup_path)
                logger.debug(f"Removed cgroup for session {session_id}")
            return True
            
        except Exception as e:
            logger.warning(f"Could not remove cgroup for session {session_id}: {e}")
        
        return False
    
    def cleanup_all(self):
        """Clean up all session cgroups"""
        for session_id in list(self.session_cgroups.keys()):
            self.remove_session_cgroup(session_id)


# Global cgroup manager instance
_cgroup_manager: Optional[CgroupManager] = None


def get_cgroup_manager() -> Optional[CgroupManager]:
    """Get or create the global cgroup manager"""
    global _cgroup_manager
    if _cgroup_manager is None:
        _cgroup_manager = CgroupManager()
        _cgroup_manager.initialize()
    return _cgroup_manager
