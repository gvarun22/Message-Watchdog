"""
Message source: Telegram group via Telethon (MTProto user client).

Why Telethon (not the Bot API)?
--------------------------------
Telethon authenticates as a real user account, allowing us to monitor any
Telegram group the user is already a member of — no bot invite or admin
permissions required. The .session file stores the auth token and must
be kept secret (never committed to git).

Session file persistence on Azure
----------------------------------
When running in Azure Container Instances, mount an Azure File Share at
/app/<session_name>.session so the session survives container restarts.
See Dockerfile and deployment notes for details.

Startup catch-up
-----------------
After connecting, the source fetches the last `startup_lookback_minutes`
worth of messages from the group and dispatches them through the normal
pipeline before switching to live streaming. This covers any messages that
arrived during a deploy restart or brief outage.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    Message as TLMessage,
)

from watchdog.core.models import Message
from watchdog.sources.base import MessageSource

logger = logging.getLogger(__name__)


def _media_type_label(media) -> Optional[str]:
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if hasattr(doc, "mime_type"):
            mime = doc.mime_type or ""
            if mime.startswith("video"):
                return "video"
            if mime.startswith("audio"):
                return "audio"
            if "gif" in mime:
                return "gif"
            if "sticker" in mime or mime == "image/webp":
                return "sticker"
        return "document"
    if isinstance(media, MessageMediaWebPage):
        return "link_preview"
    return "media"


class TelegramSource(MessageSource):
    """
    Connects to Telegram as a user account and forwards messages from a
    specific group to all registered engine callbacks.

    The `client` property exposes the underlying TelegramClient so that
    TelegramSelfAlert can reuse the same authenticated session without
    creating a second client (which would lock the SQLite session file).
    """

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        session_name: str,
        group: str,
        startup_lookback_minutes: int = 10,
        periodic_catch_up_minutes: int = 5,
    ) -> None:
        super().__init__()
        self._group = group
        self._phone = phone
        self._startup_lookback_minutes = startup_lookback_minutes
        self._periodic_catch_up_minutes = periodic_catch_up_minutes
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._chat_name: str = group
        self._seen_ids: set[int] = set()
        self._catchup_task: Optional[asyncio.Task] = None

    @property
    def source_type(self) -> str:
        return "telegram"

    @property
    def client(self) -> TelegramClient:
        """Expose the authenticated client for reuse by TelegramSelfAlert."""
        return self._client

    async def start(self) -> None:
        logger.info("TelegramSource: connecting…")
        await self._client.start(phone=lambda: self._phone)

        entity = await self._client.get_entity(self._group)
        self._chat_name = getattr(entity, "title", self._group)
        logger.info("TelegramSource: monitoring '%s'", self._chat_name)

        if self._startup_lookback_minutes > 0:
            await self._catch_up(entity, self._startup_lookback_minutes)

        @self._client.on(events.NewMessage(chats=[entity]))
        async def _handler(event: events.NewMessage.Event) -> None:
            msg_id = event.message.id
            if msg_id not in self._seen_ids:
                self._seen_ids.add(msg_id)
                await self._dispatch(self._convert(event.message))

        if self._periodic_catch_up_minutes > 0:
            self._catchup_task = asyncio.create_task(
                self._periodic_catch_up_loop(entity)
            )

        await self._client.run_until_disconnected()

    async def stop(self) -> None:
        if self._catchup_task is not None:
            self._catchup_task.cancel()
        await self._client.disconnect()
        logger.info("TelegramSource: disconnected from '%s'", self._chat_name)

    async def _periodic_catch_up_loop(self, entity) -> None:
        """Re-fetch recent messages every N minutes to recover any that the
        real-time event handler missed (e.g. due to Telethon update gaps)."""
        interval = self._periodic_catch_up_minutes * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._catch_up(entity, self._periodic_catch_up_minutes + 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("TelegramSource: periodic catch-up error: %s", exc)

    async def _catch_up(self, entity, lookback_minutes: int) -> None:
        """
        Fetch messages from the last `lookback_minutes` and dispatch any not
        already seen through the pipeline oldest-first.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        caught: list[TLMessage] = []

        async for msg in self._client.iter_messages(entity, limit=500):
            if msg.date.replace(tzinfo=timezone.utc) < cutoff:
                break
            if msg.id not in self._seen_ids:
                caught.append(msg)

        if not caught:
            return

        caught.reverse()  # oldest-first so the engine sees them in order
        for msg in caught:
            self._seen_ids.add(msg.id)
            await self._dispatch(self._convert(msg))

        logger.info(
            "TelegramSource: catch-up dispatched %d missed message(s) (last %d min)",
            len(caught), lookback_minutes,
        )

    def _convert(self, tg_msg: TLMessage) -> Message:
        """
        Convert a raw Telethon Message to the normalised Message type.
        Works for both live NewMessage events (pass event.message) and
        historical messages from iter_messages.
        """
        sender_name = None
        try:
            sender = tg_msg.sender
            if sender:
                parts = [
                    getattr(sender, "first_name", None) or "",
                    getattr(sender, "last_name", None) or "",
                ]
                full = " ".join(p for p in parts if p).strip()
                sender_name = full or getattr(sender, "username", None)
        except Exception:
            pass

        media = tg_msg.media
        has_media = media is not None and not isinstance(media, MessageMediaWebPage)
        media_label = _media_type_label(media) if has_media else None

        ts = tg_msg.date
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        return Message(
            source_type="telegram",
            chat_id=str(tg_msg.chat_id),
            chat_name=self._chat_name,
            sender_id=str(tg_msg.sender_id or "unknown"),
            sender_name=sender_name,
            text=tg_msg.text or None,
            has_media=has_media,
            media_type=media_label,
            timestamp=ts,
            raw=tg_msg,
        )
