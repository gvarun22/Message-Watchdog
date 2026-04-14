"""
Abstract base class for all message sources.

To add a new source (e.g. WhatsApp via whatsapp-web.js bridge, Slack, Discord):
  1. Create a new module in watchdog/sources/
  2. Subclass MessageSource and implement start(), stop(), and source_type
  3. Register the source type name in main.py's SOURCE_REGISTRY
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from watchdog.core.models import Message

# Type alias for the callback that engines register with a source
MessageCallback = Callable[[Message], Awaitable[None]]


class MessageSource(ABC):
    """
    A source continuously produces Message objects and delivers them to
    one or more registered callbacks (one per active WatchdogEngine).

    Lifecycle
    ---------
    1. main.py calls source.register(callback) for each engine that
       should receive messages from this source.
    2. main.py calls await source.start() — this blocks until the source
       disconnects (or asyncio.CancelledError is raised on shutdown).
    3. On shutdown, main.py calls await source.stop().
    """

    def __init__(self) -> None:
        self._callbacks: list[MessageCallback] = []

    def register(self, callback: MessageCallback) -> None:
        """Register a callback to receive messages from this source."""
        self._callbacks.append(callback)

    async def _dispatch(self, message: Message) -> None:
        """Fan out a message to all registered callbacks."""
        for cb in self._callbacks:
            await cb(message)

    @abstractmethod
    async def start(self) -> None:
        """
        Begin listening. This coroutine runs until disconnected.
        Call _dispatch() for every incoming message.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully disconnect from the source."""
        ...

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Short lowercase identifier, e.g. 'telegram'."""
        ...
