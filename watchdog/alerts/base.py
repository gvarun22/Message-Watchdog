"""
Abstract base class for all alert delivery channels.

To add a new channel (e.g. Slack, PagerDuty, SMS):
  1. Create a new module in watchdog/alerts/
  2. Subclass AlertChannel and implement send() and channel_name
  3. Register the channel name in main.py's ALERT_CHANNEL_REGISTRY
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from watchdog.core.models import ClassificationResult


class AlertChannel(ABC):
    """
    Delivers an alert through a specific channel (phone call, email, etc.).

    The send() method MUST NOT raise — all exceptions must be caught internally
    and logged. A failure in one channel must never prevent other channels from
    firing (they are dispatched concurrently via asyncio.gather with
    return_exceptions=True, but defensive coding inside send() is still required).

    config_name is set by main.py after construction and matches the key used
    in WatchdogConfig.channel_thresholds (e.g. "phone_call", "email").
    """
    config_name: str = ""

    @abstractmethod
    async def send(self, message: str, result: ClassificationResult) -> None:
        """
        Deliver the alert.

        Parameters
        ----------
        message:
            Pre-formatted human-readable alert text.
        result:
            Structured classification result for building rich alert content.
        """
        ...

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """Human-readable identifier used in log messages."""
        ...
