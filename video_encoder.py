"""
Video Encoder - Simplified (GPU encoding removed)
Only keeps AdaptiveBitrateController for network monitoring
"""

import time
from collections import deque


class AdaptiveBitrateController:
    """
    Adaptive bitrate controller that adjusts quality based on network conditions
    Monitors client feedback for quality adjustments
    """

    def __init__(self, config):
        self.config = config
        self.current_bitrate = getattr(config, 'bitrate_default', 15000000)
        self.latency_history = deque(maxlen=30)
        self.frame_drop_history = deque(maxlen=30)
        self.last_adjustment = time.time()

    def update_client_feedback(self, latency_ms: float, frame_drop_percent: float):
        """Update with client feedback metrics"""
        self.latency_history.append(latency_ms)
        self.frame_drop_history.append(frame_drop_percent)

    def calculate_adjusted_bitrate(self) -> int:
        """Calculate optimal bitrate based on network conditions"""
        avg_latency = sum(self.latency_history) / len(self.latency_history) if self.latency_history else 50
        avg_drop_rate = sum(self.frame_drop_history) / len(self.frame_drop_history) if self.frame_drop_history else 0

        bitrate_min = getattr(self.config, 'bitrate_min', 10000000)
        bitrate_max = getattr(self.config, 'bitrate_max', 20000000)

        if avg_latency > 150 or avg_drop_rate > 10:
            self.current_bitrate = max(bitrate_min, self.current_bitrate - 2000000)
        elif avg_latency < 50 and avg_drop_rate < 2:
            self.current_bitrate = min(bitrate_max, self.current_bitrate + 1000000)

        return self.current_bitrate
