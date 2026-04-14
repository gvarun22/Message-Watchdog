"""
Sliding-window message rate tracker.

Maintains a deque of UTC timestamps and computes:
  - current_rate  : messages/min over the short window (e.g. 60 s)
  - baseline_rate : messages/min over the long window  (e.g. 1 hour)
  - spike_factor  : current_rate / max(baseline_rate, 0.01)

These three numbers are passed to the LLM as activity context.
The spike_factor is the most valuable signal: a burst of messages in
an otherwise quiet group is a strong indicator that something notable
is happening, even when individual messages are ambiguous.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone

from watchdog.core.models import ActivityContext


class ActivityTracker:
    """
    Thread-safe within a single asyncio event loop (no locks needed because
    asyncio is single-threaded and all coroutines share the same loop).
    """

    def __init__(
        self,
        short_window_seconds: int = 60,
        long_window_seconds: int = 3600,
    ) -> None:
        self.short_window = short_window_seconds
        self.long_window = long_window_seconds
        # Stores UTC timestamps of all messages within the long window.
        # Ordered chronologically; popleft() is O(1).
        self._timestamps: deque[datetime] = deque()

    def record(self, timestamp: datetime | None = None) -> None:
        """
        Record a message arrival. Call this for EVERY message, not just
        scored ones, because raw volume is itself a signal.
        """
        ts = timestamp or datetime.now(timezone.utc)
        self._timestamps.append(ts)
        self._prune(ts)

    def get_context(self) -> ActivityContext:
        """
        Return the current activity snapshot without recording a new message.
        Useful for logging and diagnostics.
        """
        now = datetime.now(timezone.utc)
        self._prune(now)

        short_cutoff = now - timedelta(seconds=self.short_window)
        short_count = sum(1 for t in self._timestamps if t >= short_cutoff)
        long_count = len(self._timestamps)

        current_rate = (short_count / self.short_window) * 60   # per minute
        baseline_rate = (long_count / self.long_window) * 60    # per minute
        spike_factor = current_rate / max(baseline_rate, 0.01)

        return ActivityContext(
            current_rate=round(current_rate, 2),
            baseline_rate=round(baseline_rate, 2),
            spike_factor=round(spike_factor, 2),
            window_seconds=self.short_window,
        )

    def _prune(self, now: datetime) -> None:
        """Remove timestamps older than the long window."""
        cutoff = now - timedelta(seconds=self.long_window)
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
