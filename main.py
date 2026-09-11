"""
Main Entry Point - Neo Browser Streaming Server
Pure CDP Screencast - No GPU encoding required
With Memory Optimization, Swap Space Management, and Cgroup Isolation

Modular structure:
- telegram_bot.py: Telegram bot functionality
- memory_manager.py: Memory management and cgroup isolation
- gpu_manager.py: GPU/resource management
- api.py: FastAPI REST endpoints and WebSocket handlers
"""

import asyncio
import sys
import signal
import os
import subprocess
import logging
import time
import platform
from typing import Optional

# Add current directory to path
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

# Auto-load .env if present, BEFORE importing config so the env-var
# mappings pick up the values. We do a minimal dotenv loader here so
# we don't add python-dotenv as a hard requirement. If you don't have
# a .env, this is a no-op.
def _load_dotenv(path: Path) -> int:
    """Read KEY=VALUE lines from a .env file and put them in os.environ
    if not already set. Returns the number of vars loaded. Skips blanks
    and comments.
    """
    if not path.exists():
        return 0
    loaded = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                # Strip surrounding quotes
                if (v.startswith('"') and v.endswith('"')) or \
                   (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                if k and k not in os.environ:
                    os.environ[k] = v
                    loaded += 1
    except Exception:
        pass
    return loaded

_loaded = _load_dotenv(Path(__file__).parent / ".env")
if _loaded:
    logging.basicConfig(level=getattr(logging, (os.environ.get("LOG_LEVEL") or "ERROR").upper(), logging.ERROR), force=False)
    logging.getLogger(__name__).debug(f"[env] loaded {_loaded} vars from .env")

import uvicorn

from config import CONFIG, reload_config
from gpu_manager import GPUManager
from session_manager import SessionManager
from api import app, set_session_manager, set_server_instance

# Import from modular components
from telegram_bot import TelegramBot
from memory_manager import (
    is_windows, is_linux,
    check_and_setup_memory,
    get_cgroup_manager
)


def _print_secret_status() -> None:
    """Log which sensitive env-driven secrets are present vs missing.
    Called once on startup so the operator sees a clear status banner
    instead of a mid-session 'Proxy not configured properly' warning.
    """
    import logging
    log = logging.getLogger("startup")
    log.debug("=" * 60)
    log.debug("[startup] Secret / proxy status (set in .env or env vars):")
    log.debug("=" * 60)

    def _row(name: str, env_var: str, value: str, required: bool = False) -> None:
        status = "OK   " if value else "MISSING"
        tag = " (REQUIRED)" if required and not value else ""
        shown = (value[:6] + "..." + value[-4:]) if len(value) > 14 else (value or "<empty>")
        log.debug(f"  [{status}] {name:35s} via {env_var:35s} = {shown}{tag}")

    _row("Telegram bot token", "TELEGRAM_BOT_TOKEN", CONFIG.telegram_bot_token or "",
         required=False)
    _row("Telegram chat id",   "TELEGRAM_CHAT_ID",   CONFIG.telegram_chat_id or "")
    _row("Telegram admin pwd", "TELEGRAM_ADMIN_PASSWORD", CONFIG.telegram_admin_password or "")
    _row("Decodo proxy user",  "PROXY_USERNAME",     CONFIG.proxy_username or "")
    _row("Decodo proxy pass",  "PROXY_PASSWORD",     CONFIG.proxy_password or "")
    _row("Oxylabs DC user",    "OXYLABS_BROWSER_USERNAME", CONFIG.oxylabs_browser_username or "")
    _row("Oxylabs Web Unlock", "OXYLABS_UNLOCKER_USERNAME", CONFIG.oxylabs_unlocker_username or "")

    if not CONFIG.proxy_username:
        log.warning(
            "[startup] No PROXY_USERNAME set. Browser sessions will run "
            "with the server's own IP (datacenter). Google / Cloudflare / "
            "DataDome will flag these as bot traffic. Set PROXY_USERNAME "
            "+ PROXY_PASSWORD in .env to enable proxy routing."
        )
    if CONFIG.telegram_enabled and not CONFIG.telegram_bot_token:
        log.warning(
            "[startup] Telegram enabled but TELEGRAM_BOT_TOKEN is empty. "
            "Admin notifications will be disabled. Set TELEGRAM_BOT_TOKEN "
            "in .env to enable."
        )
    log.debug("=" * 60)

# Configure logging — ERRORS ONLY by default so the production console stays
# clean and the stream never stutters under log spam.  Set LOG_LEVEL=DEBUG
# (env or .env) to re-enable the full trace when diagnosing.
_LOG_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "ERROR").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.ERROR)
logging.basicConfig(level=_LOG_LEVEL)
logger = logging.getLogger(__name__)

# Every module (ours + third-party) follows the same level; nothing is
# allowed to be louder than the root.
for mod in ["httpx", "httpcore", "uvicorn", "uvicorn.access", "uvicorn.error",
            "asyncio", "websockets", "playwright",
            "api", "cdp_screencast_stream", "session", "session_manager",
            "browser_manager", "dom_capture", "telegram_bot", "memory_manager",
            "gpu_manager", "pcm_manager", "webrtc_stream", "frame_crop",
            "lpv_store", "archiver", "aioice_patch"]:
    logging.getLogger(mod).setLevel(_LOG_LEVEL)


class Server:
    """Main server class with lifecycle management"""

    def __init__(self):
        self.config = CONFIG
        self.gpu_manager = GPUManager(self.config)
        self.session_manager = SessionManager(self.config, self.gpu_manager)
        self.shutdown_event = asyncio.Event()
        self.cloudflare_process = None
        self.server = None
        self.telegram_bot = None

    def setup_signal_handlers(self):
        """Setup graceful shutdown handlers"""
        def signal_handler(signum, frame):
            asyncio.create_task(self.shutdown())

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

    async def start(self):
        """Start the server"""

        # Setup signal handlers
        self.setup_signal_handlers()

        # Print secret / proxy status banner BEFORE anything else so
        # the operator sees config problems immediately, not mid-session.
        _print_secret_status()

        # ==============================
        # Memory Management & Cgroup Setup
        # ==============================
        
        # Check and setup swap space if needed
        logger.debug("Checking system memory configuration...")
        check_and_setup_memory()
        
        # Initialize cgroup manager for session isolation (Linux only)
        if is_linux():
            logger.debug("Initializing cgroup session isolation...")
            cgroup_mgr = get_cgroup_manager()
            if cgroup_mgr and cgroup_mgr._initialized:
                logger.debug("Cgroup session isolation enabled")
            else:
                logger.debug("Cgroup not available, running without session isolation")
        else:
            logger.debug("Cgroup not available on Windows, running without session isolation")
        
        # GPU detection and resource status
        gpu_status = self.gpu_manager.get_status()
        
        # Log resource status
        logger.debug(f"[Startup] System Resources:")
        logger.debug(f"  - Active Sessions: {gpu_status.get('active_sessions', 0)}/{gpu_status.get('max_sessions', '?')}")
        logger.debug(f"  - Memory Available: {gpu_status.get('memory_available_mb', '?'):.0f}MB")
        logger.debug(f"  - Memory Used: {gpu_status.get('memory_used_percent', '?'):.1f}%")
        logger.debug(f"  - Disk Free: {gpu_status.get('disk_free_gb', '?'):.1f}GB")
        logger.debug(f"  - System Healthy: {gpu_status.get('system_healthy', '?')}")
        
        # Cleanup orphaned Chrome processes at startup
        logger.debug("[Startup] Cleaning up orphaned Chrome processes...")
        killed = self.gpu_manager.cleanup_orphaned_chrome_processes()
        if killed > 0:
            logger.debug(f"[Startup] Cleaned up {killed} orphaned Chrome processes")

        # Set session manager in API
        set_session_manager(self.session_manager)
        
        # Set server instance for restart functionality
        set_server_instance(self)

        # Start session manager
        await self.session_manager.start()

        # Start Cloudflare tunnel if enabled
        if self.config.cloudflare_tunnel:
            await self.start_cloudflare_tunnel()

        # Initialize and start Telegram bot
        if getattr(CONFIG, 'telegram_enabled', False):
            self.telegram_bot = TelegramBot(self.config, server_instance=self)
            self.telegram_bot.set_shutdown_event(self.shutdown_event)
            await self.telegram_bot.start_polling()

        # Start server
        config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_level="error",
            ws_ping_interval=None,
            ws_ping_timeout=None,
            workers=1,
            loop="uvloop",
        )

        self.server = uvicorn.Server(config)
        await self.server.serve()

        await self.shutdown_event.wait()

    async def shutdown(self):
        """Graceful shutdown"""
        pass

        # Stop Telegram bot
        if self.telegram_bot:
            await self.telegram_bot.stop_polling()

        if hasattr(self, 'server'):
            self.server.should_exit = True

        await self.session_manager.close_all_sessions()
        await self.session_manager.stop()

        await self.stop_cloudflare_tunnel()
        
        # Clean up cgroups (Linux only)
        if is_linux():
            cgroup_mgr = get_cgroup_manager()
            if cgroup_mgr:
                logger.debug("Cleaning up session cgroups...")
                cgroup_mgr.cleanup_all()

        self.shutdown_event.set()

    async def restart(self):
        """Full restart - forcefully kills ALL browser processes and restarts completely"""
        logger.debug("Full server restart initiated - killing all processes...")
        
        # Step 1: Forcefully close all sessions and browsers
        try:
            await self.session_manager.close_all_sessions(force=True)
        except Exception as e:
            logger.error(f"Error closing sessions: {e}")
        
        try:
            await self.session_manager.stop()
        except Exception as e:
            logger.error(f"Error stopping session manager: {e}")
        
        # Step 2: Kill all Chrome/Chromium processes forcefully
        try:
            if is_windows():
                # Windows: Use taskkill
                subprocess.run(['taskkill', '/F', '/IM', 'chrome.exe'], capture_output=True)
                subprocess.run(['taskkill', '/F', '/IM', 'chromium.exe'], capture_output=True)
                logger.debug("All browser processes killed (Windows)")
            else:
                # Linux: Use pkill
                subprocess.run(['pkill', '-9', '-f', 'chrome'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'chromium'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'chrome-linux'], capture_output=True)
                subprocess.run(['pkill', '-9', '-f', 'headless_shell'], capture_output=True)
                logger.debug("All browser processes killed (Linux)")
        except Exception as e:
            logger.error(f"Error killing browser processes: {e}")
        
        # Step 3: Stop cloudflare tunnel
        try:
            await self.stop_cloudflare_tunnel()
        except Exception as e:
            logger.error(f"Error stopping tunnel: {e}")
        
        # Step 4: Clear any lingering processes on the port
        try:
            port = self.config.port
            if is_windows():
                # Windows: Use netstat and taskkill
                result = subprocess.run(
                    ['netstat', '-ano'],
                    capture_output=True,
                    text=True
                )
                for line in result.stdout.split('\n'):
                    if f':{port}' in line and 'LISTENING' in line:
                        parts = line.split()
                        if len(parts) > 4:
                            pid = parts[-1]
                            try:
                                subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True)
                            except Exception:
                                pass
            else:
                # Linux: Use fuser
                subprocess.run(['fuser', '-k', f'{port}/tcp'], capture_output=True)
            await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error clearing port: {e}")
        
        # Step 5: Signal server to exit with restart code
        if hasattr(self, 'server'):
            self.server.should_exit = True
        
        self.shutdown_event.set()
        
        # Return restart code (42 signals main() to restart)
        return 42

    async def start_cloudflare_tunnel(self):
        """Start Cloudflare tunnel for public access"""
        if not self.config.tunnel_hostname:
            return

        try:
            process = await asyncio.create_subprocess_exec(
                'which', 'cloudflared',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.wait()
            if process.returncode != 0:
                return

            self.cloudflare_process = await asyncio.create_subprocess_exec(
                'cloudflared', 'tunnel',
                '--url', f'http://localhost:{self.config.port}',
                '--hostname', self.config.tunnel_hostname,
                '--metrics', '0.0.0.0:4040',
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

        except FileNotFoundError:
            pass  # cloudflared not installed - normal on some systems
        except Exception as e:
            logger.error(f"[Cloudflare Error] {e}")

    async def stop_cloudflare_tunnel(self):
        """Stop Cloudflare tunnel"""
        if self.cloudflare_process:
            self.cloudflare_process.terminate()
            try:
                await asyncio.wait_for(self.cloudflare_process.wait(), timeout=5.0)
            except asyncio.TimeoutExpired:
                self.cloudflare_process.kill()
            self.cloudflare_process = None


async def kill_port_processes(port: int):
    """Kill any existing processes on the specified port"""
    try:
        if is_windows():
            # Windows: Use netstat and taskkill
            result = subprocess.run(
                ['netstat', '-ano'],
                capture_output=True,
                text=True
            )
            for line in result.stdout.split('\n'):
                if f':{port}' in line and 'LISTENING' in line:
                    parts = line.split()
                    if len(parts) > 4:
                        pid = parts[-1]
                        try:
                            subprocess.run(['taskkill', '/F', '/PID', pid], capture_output=True)
                        except Exception:
                            pass
            await asyncio.sleep(1)
        else:
            # Linux: Use fuser
            result = subprocess.run(
                ['fuser', '-k', f'{port}/tcp'],
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                await asyncio.sleep(1)
            else:
                result = subprocess.run(
                    ['lsof', '-ti', f':{port}'],
                    capture_output=True,
                    text=True
                )

                if result.stdout.strip():
                    pids = result.stdout.strip().split('\n')
                    for pid in pids:
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except (ValueError, ProcessLookupError):
                            pass

    except FileNotFoundError:
        pass
    except Exception:
        pass


async def check_port_available(port: int) -> bool:
    """Check if a port is available"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('', port))
            return True
        except OSError:
            return False


async def main():
    """Main entry point with restart support"""
    port = CONFIG.port

    while True:
        # Kill existing processes on port
        for attempt in range(3):
            await kill_port_processes(port)
            await asyncio.sleep(2)
            if await check_port_available(port):
                break

        # Create server instance
        server = Server()

        # Start server
        try:
            exit_code = await server.start()
            # If exit_code is 42, restart the server
            if exit_code == 42:
                logger.debug("Restarting server...")
                await asyncio.sleep(2)
                continue
            break
        except KeyboardInterrupt:
            break
        except Exception:
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
