"""
WatchdogEngine — one instance per WatchdogConfig.

Responsibilities
----------------
1. Accept messages from sources via an asyncio.Queue
2. Buffer messages per chat_id in a _BufferedChat slot
3. Flush the buffer to the LLM classifier when:
   a) batch_window_seconds elapse with no new messages (debounce timer)
   b) the buffer reaches batch_burst_cap messages (immediate burst flush)
4. Forward activity context (spike_factor) to the classifier
5. Fire alert channels when the classifier triggers (confidence >= threshold)
6. Enforce a cooldown period to prevent alert spam

Design notes
------------
- One WatchdogEngine per WatchdogConfig, not per source. The same source
  fan-outs to multiple engines (e.g. H1B engine + H4 engine on same group).
- asyncio.Queue is the intake point; sources push messages without knowing
  engine internals.
- Cooldown is in-memory only. A process restart resets it intentionally —
  restarting during an active event should re-alert immediately.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from watchdog.alerts.base import AlertChannel
from watchdog.core.activity_tracker import ActivityTracker
from watchdog.core.classifier import LLMClassifier
from watchdog.core.models import ActivityContext, ClassificationResult, Message, WatchdogConfig
from watchdog.core.surge_gate import SurgeGate

logger = logging.getLogger(__name__)


@dataclass
class _BufferedChat:
    """Couples the message buffer and its debounce timer for one chat_id."""
    messages: list[Message] = field(default_factory=list)
    timer: Optional[asyncio.TimerHandle] = None


def _build_alert_text(
    config: WatchdogConfig,
    messages: list[Message],
    result: ClassificationResult,
    activity_context: ActivityContext,
    now: datetime,
) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    key_signals_block = (
        "\n".join(f"  • {s}" for s in result.key_signals)
        if result.key_signals
        else "  (none listed)"
    )
    messages_block = "\n".join(m.format_for_log() for m in messages[-5:])
    return (
        f"WATCHDOG ALERT — {config.name}\n"
        f"{'=' * 50}\n"
        f"Time       : {ts}\n"
        f"Confidence : {int(result.confidence * 100)}%\n"
        f"Model      : {result.model_used}\n"
        f"Rate spike : {activity_context.spike_factor:.1f}x baseline\n"
        f"\nReason:\n  {result.reason}\n"
        f"\nKey signals:\n{key_signals_block}\n"
        f"\nRecent messages ({config.source_group}):\n{messages_block}\n"
        f"\nACTION: Open your app NOW and check for available slots!"
    )


class WatchdogEngine:
    """
    Consumes messages from an asyncio.Queue, batches them, and triggers
    LLM classification when the batch is ready to evaluate.
    """

    def __init__(
        self,
        config: WatchdogConfig,
        classifier: LLMClassifier,
        alert_channels: list[AlertChannel],
    ) -> None:
        self.config = config
        self._classifier = classifier
        self._alert_channels = alert_channels
        self._tracker = ActivityTracker()
        self._surge_gate = SurgeGate(config.surge_gate)
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self._chats: dict[str, _BufferedChat] = {}
        self._last_alert_time: Optional[datetime] = None
        self._alert_count: int = 0
        self._running = False

    async def run(self) -> None:
        self._running = True
        logger.info("WatchdogEngine '%s' started", self.config.name)
        while self._running:
            try:
                msg = await asyncio.wait_for(self.queue.get(), timeout=1.0)
                self._tracker.record(msg.timestamp)
                await self._buffer_message(msg)
                self.queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unhandled error in WatchdogEngine '%s'", self.config.name)
        logger.info("WatchdogEngine '%s' stopped", self.config.name)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Buffering
    # ------------------------------------------------------------------

    async def _buffer_message(self, msg: Message) -> None:
        chat = self._chats.setdefault(msg.chat_id, _BufferedChat())
        chat.messages.append(msg)

        if chat.timer is not None:
            chat.timer.cancel()

        if len(chat.messages) >= self.config.batch_burst_cap:
            logger.debug(
                "[%s] Burst cap hit (%d msgs) — flushing immediately",
                self.config.name, len(chat.messages),
            )
            await self._flush(msg.chat_id)
            return

        loop = asyncio.get_running_loop()
        chat.timer = loop.call_later(
            self.config.batch_window_seconds,
            lambda cid=msg.chat_id: asyncio.ensure_future(self._flush(cid)),
        )

    async def _flush(self, chat_id: str) -> None:
        chat = self._chats.get(chat_id)
        if not chat or not chat.messages:
            return

        messages = chat.messages
        # Reset the slot — keeps the key so future messages don't re-allocate,
        # but avoids unbounded growth by clearing the list in-place.
        chat.messages = []
        if chat.timer is not None:
            chat.timer.cancel()
            chat.timer = None

        activity_ctx = self._tracker.get_context()

        if not self._surge_gate.should_classify(messages, activity_ctx, self.config.name):
            return

        logger.debug(
            "[%s] Classifying batch of %d messages (spike=%.1fx)",
            self.config.name, len(messages), activity_ctx.spike_factor,
        )

        result = await self._classifier.classify(
            messages=messages,
            condition=self.config.condition,
            activity_context=activity_ctx,
            chat_name=messages[0].chat_name,
        )

        logger.info(
            "[%s] Classification: triggered=%s confidence=%.2f | %s",
            self.config.name, result.triggered, result.confidence, result.reason[:120],
        )

        if result.triggered and result.confidence >= self.config.confidence_threshold:
            await self._maybe_alert(messages, result, activity_ctx)

    # ------------------------------------------------------------------
    # Alert dispatch
    # ------------------------------------------------------------------

    async def _maybe_alert(
        self,
        messages: list[Message],
        result: ClassificationResult,
        activity_ctx: ActivityContext,
    ) -> None:
        now = datetime.now(timezone.utc)

        if self._last_alert_time is not None:
            elapsed = (now - self._last_alert_time).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                logger.info(
                    "[%s] Alert suppressed — cooldown active (%.0fs remaining)",
                    self.config.name, self.config.cooldown_seconds - elapsed,
                )
                return

        self._last_alert_time = now
        self._alert_count += 1
        alert_text = _build_alert_text(self.config, messages, result, activity_ctx, now)

        if self.config.dry_run:
            logger.warning(
                "[%s] DRY RUN — alert #%d would fire:\n%s",
                self.config.name, self._alert_count, alert_text,
            )
            return

        logger.warning(
            "[%s] Firing alert #%d (confidence=%.2f)",
            self.config.name, self._alert_count, result.confidence,
        )
        channels_to_fire = [
            ch for ch in self._alert_channels
            if result.confidence >= self.config.channel_thresholds.get(
                ch.config_name, self.config.confidence_threshold
            )
        ]
        suppressed = len(self._alert_channels) - len(channels_to_fire)
        if suppressed:
            logger.info(
                "[%s] %d channel(s) suppressed by per-channel threshold",
                self.config.name, suppressed,
            )
        await asyncio.gather(
            *[ch.send(alert_text, result) for ch in channels_to_fire],
            return_exceptions=True,
        )
