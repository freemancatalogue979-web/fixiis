"""
Frame Pool - Pre-allocated zero-copy buffers for high-FPS streaming
Eliminates memory allocation overhead during streaming
"""

import asyncio
from typing import Optional
from dataclasses import dataclass, field
from collections import deque
import time


@dataclass
class Frame:
    """A single frame with pre-allocated buffer."""
    buffer: bytearray = field(default_factory=lambda: bytearray(1920 * 1080 * 4))  # 1080p BGRA
    number: int = 0
    timestamp: float = 0.0
    width: int = 1920
    height: int = 1080
    in_use: bool = False

    def reset(self):
        """Reset frame for reuse."""
        self.number = 0
        self.timestamp = 0.0
        self.in_use = False


class FramePool:
    """
    Pre-allocated pool of frame buffers.
    Eliminates allocation overhead during streaming.
    """

    def __init__(self, size: int = 128, width: int = 1920, height: int = 1080):
        """
        Initialize frame pool.

        Args:
            size: Number of pre-allocated frames (increased to 128 for 60 FPS 4K)
            width: Frame width in pixels
            height: Frame height in pixels
        """
        self.size = size
        self.width = width
        self.height = height
        self.frame_size = width * height * 4  # BGRA = 4 bytes per pixel

        # Pre-allocate all frames
        self.frames = deque()
        for i in range(size):
            frame = Frame(
                buffer=bytearray(self.frame_size),
                width=width,
                height=height
            )
            self.frames.append(frame)

        # Semaphore for pool access (prevents exhaustion)
        self.available = asyncio.Semaphore(size)

        # Statistics
        self.acquired_count = 0
        self.released_count = 0
        self.wait_count = 0

    async def acquire(self, timeout: float = 0.01) -> Optional[Frame]:
        """
        Acquire a frame from the pool.

        Args:
            timeout: Maximum time to wait for available frame

        Returns:
            Frame or None if timeout
        """
        try:
            await asyncio.wait_for(self.available.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            self.wait_count += 1
            return None

        # Get frame from pool
        frame = self.frames.popleft()
        frame.in_use = True
        self.acquired_count += 1
        return frame

    def release(self, frame: Frame):
        """
        Return a frame to the pool.

        Args:
            frame: Frame to return
        """
        if not frame.in_use:
            return  # Already released

        frame.reset()
        self.frames.append(frame)
        self.available.release()
        self.released_count += 1

    def get_stats(self) -> dict:
        """Get pool statistics."""
        return {
            'size': self.size,
            'available': self.available._value,
            'acquired': self.acquired_count,
            'released': self.released_count,
            'waits': self.wait_count,
            'in_use': self.acquired_count - self.released_count,
        }


class PacketPool:
    """
    Pre-allocated pool of network packet buffers.
    Reduces allocation overhead for WebSocket sends.
    """

    def __init__(self, size: int = 64, max_packet_size: int = 512 * 1024):
        """
        Initialize packet pool.

        Args:
            size: Number of pre-allocated packets (increased for 60 FPS)
            max_packet_size: Maximum packet size in bytes
        """
        self.size = size
        self.max_packet_size = max_packet_size

        # Pre-allocate all packets
        self.packets = deque()
        for _ in range(size):
            self.packets.append(bytearray(max_packet_size))

        # Semaphore for pool access
        self.available = asyncio.Semaphore(size)

        # Statistics
        self.acquired_count = 0
        self.released_count = 0

    async def acquire(self, timeout: float = 0.01) -> Optional[bytearray]:
        """Acquire a packet buffer."""
        try:
            await asyncio.wait_for(self.available.acquire(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

        packet = self.packets.popleft()
        self.acquired_count += 1
        return packet

    def release(self, packet: bytearray):
        """Return a packet to the pool."""
        self.packets.append(packet)
        self.available.release()
        self.released_count += 1


class StreamingPipeline:
    """
    Main streaming pipeline coordinator.
    Implements frame skipping and backpressure control.
    """

    def __init__(self, target_fps: int = 60):
        self.target_fps = target_fps
        self.target_frame_time = 1.0 / target_fps
        self.max_queue_depth = 2  # Skip frames if queue exceeds this

        # Counters
        self.frame_number = 0
        self.dropped_frames = 0
        self.total_frames = 0

        # Timing
        self.last_frame_time = time.time()

    def should_skip_frame(self, queue_depth: int) -> bool:
        """
        Determine if frame should be skipped.

        Args:
            queue_depth: Current encode queue depth

        Returns:
            True if frame should be skipped
        """
        if queue_depth >= self.max_queue_depth:
            self.dropped_frames += 1
            return True
        return False

    def get_next_frame_number(self) -> int:
        """Get next frame number."""
        self.frame_number += 1
        self.total_frames += 1
        return self.frame_number

    def get_drop_rate(self) -> float:
        """Calculate frame drop rate."""
        if self.total_frames == 0:
            return 0.0
        return self.dropped_frames / self.total_frames

    def get_stats(self) -> dict:
        """Get pipeline statistics."""
        return {
            'target_fps': self.target_fps,
            'frame_number': self.frame_number,
            'total_frames': self.total_frames,
            'dropped_frames': self.dropped_frames,
            'drop_rate': self.get_drop_rate(),
        }


# NOTE: Singleton pattern removed - each session creates its own pools
# Pools are now independent per session to avoid resource contention
# 
# OLD SINGLETON CODE (commented out):
# _frame_pool = None
# _packet_pool = None
# _pipeline = None


# def get_frame_pool() -> FramePool:
#     """Get global frame pool instance."""
#     global _frame_pool
#     if _frame_pool is None:
#         _frame_pool = FramePool()
#     return _frame_pool


# def get_packet_pool() -> PacketPool:
#     """Get global packet pool instance."""
#     global _packet_pool
#     if _packet_pool is None:
#         _packet_pool = PacketPool()
#     return _packet_pool


# def get_pipeline() -> StreamingPipeline:
#     """Get global pipeline instance."""
#     global _pipeline
#     if _pipeline is None:
#         _pipeline = StreamingPipeline()
#     return _pipeline
