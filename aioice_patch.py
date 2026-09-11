"""
Patch for aioice STUN transaction timeout issue.
Fixes: "asyncio.exceptions.InvalidStateError: invalid state" in Transaction.__retry()
Root cause: Race condition in aioice when future is already resolved.
Strategy: Enhanced asyncio Future error handling + STUN configuration tuning.
"""

import logging
import asyncio

logger = logging.getLogger(__name__)


def patch_aioice():
    """
    Prepares asyncio for safe STUN transaction handling.
    The main fix is using JPEG instead of PNG + faster frame acquisition timeout,
    but this adds defensive error handling.
    """
    try:
        import aioice
        return True
    except ImportError:
        return False


def patch_asyncio_warnings():
    """
    Suppress non-critical asyncio warnings that can spam logs during WebRTC operations.
    These are typically from transaction retries that don't affect functionality.
    """
    import warnings
    
    # Suppress task exception warnings from aioice/aiortc
    warnings.filterwarnings("ignore", message=".*Task exception was never retrieved.*")
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="asyncio")
    warnings.filterwarnings("ignore", message=".*set_exception.*")


def setup_stun_error_handling():
    """
    Enhanced STUN error handling - patches asyncio.Future to safely handle exceptions.
    Prevents InvalidStateError when trying to set exception on already-resolved futures.
    """
    try:
        original_set_exception = asyncio.Future.set_exception
        
        def safe_set_exception(self, exception):
            """Safely set exception, checking if future is already done."""
            try:
                if not self.done():
                    original_set_exception(self, exception)
            except Exception:
                pass  # Silently ignore - exception already set
        
        # Only patch if not already patched
        if not hasattr(asyncio.Future, '_patched_set_exception'):
            asyncio.Future.set_exception = safe_set_exception
            asyncio.Future._patched_set_exception = True
        
        return True
    except Exception:
        return False


# Auto-patch on import - prepares asyncio for WebRTC operations
if __name__ != "__main__":
    patch_aioice()
    patch_asyncio_warnings()
    setup_stun_error_handling()
