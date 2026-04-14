"""
Alert channel: Telegram self-message (Saved Messages).

Sends a message to the user's own "Saved Messages" chat, which triggers
an immediate push notification on all of the user's Telegram-connected devices.

IMPORTANT — session reuse
-------------------------
TelegramSelfAlert MUST receive the same TelegramClient instance that is
already being used by TelegramSource. Creating a second client with the
same .session file causes "sqlite3.OperationalError: database is locked".
main.py is responsible for passing the shared client reference here.
"""
from __future__ import annotations

import logging

from watchdog.alerts.base import AlertChannel
from watchdog.core.models import ClassificationResult

logger = logging.getLogger(__name__)


class TelegramSelfAlert(AlertChannel):
    """Sends the alert as a Telegram message to the user's Saved Messages."""

    def __init__(self, client, watchdog_name: str) -> None:  # client: TelegramClient
        self._client = client
        self._watchdog_name = watchdog_name

    @property
    def channel_name(self) -> str:
        return "TelegramSelf"

    async def send(self, message: str, result: ClassificationResult) -> None:
        try:
            # 'me' routes to Saved Messages — works regardless of username
            await self._client.send_message("me", message[:4096])  # Telegram message cap
            logger.info("[%s] TelegramSelfAlert sent.", self._watchdog_name)
        except Exception as exc:
            logger.error("[%s] TelegramSelfAlert failed: %s", self._watchdog_name, exc)
