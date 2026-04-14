"""
Surge gate — the LLM trip switch.

The surge gate is a cheap pre-filter that sits in front of the LLM classifier.
It blocks LLM calls during quiet periods and only lets batches through when
there is evidence that something worth classifying is happening.

Why this matters
----------------
In a noisy Telegram group, the vast majority of messages have nothing to do
with the watched condition. Without a gate, every 60-second batch would be
sent to the LLM — most calls returning triggered=False and wasting tokens.

With the gate, an LLM call only fires when at least ONE of these is true:
  1. Rate spike   : current message rate >= min_spike_factor × baseline
  2. High rate    : current rate >= always_classify_rate (group is busy regardless)
  3. Keyword hit  : any message in the batch matches a broad keyword pattern

The keyword patterns are intentionally over-inclusive (e.g. "h1b", "h-1b",
"h 1 b"). The LLM makes the actual accuracy decision — the gate's only job
is to prevent wasted calls when no relevant signal exists at all.

Cost impact example
-------------------
Group activity: ~100 messages/hour, mix of visa types.
Without gate : 100 msg/hr ÷ 30 burst_cap ≈ 3-4 LLM calls/hr → ~$0.05/day
With gate    : ~5 relevant batches/day → ~$0.001/day  (≈ 50× reduction)
On Fridays when slots open: gate opens wide, LLM runs on every burst.
"""
from __future__ import annotations

import logging
import re

from watchdog.core.models import ActivityContext, Message, SurgeGateConfig

logger = logging.getLogger(__name__)


class SurgeGate:
    """
    Evaluates a batch of messages and decides whether the LLM should run.
    Stateless — a new evaluation on every flush.
    """

    def __init__(self, config: SurgeGateConfig) -> None:
        self._config = config
        # Combine all patterns into one regex — O(messages) instead of O(messages × patterns)
        combined = "|".join(f"(?:{p})" for p in config.keyword_patterns)
        self._pattern = re.compile(combined, re.IGNORECASE) if combined else None

    def should_classify(
        self,
        messages: list[Message],
        activity_ctx: ActivityContext,
        watchdog_name: str = "",
    ) -> bool:
        """
        Return True if the LLM should be called for this batch, False to skip.
        """
        if not self._config.enabled:
            return True

        # Condition 1: absolute rate threshold (group is definitely active)
        if activity_ctx.current_rate >= self._config.always_classify_rate:
            logger.debug(
                "[%s] SurgeGate OPEN — high rate %.1f msg/min",
                watchdog_name, activity_ctx.current_rate,
            )
            return True

        # Condition 2: spike above baseline
        if activity_ctx.spike_factor >= self._config.min_spike_factor:
            logger.debug(
                "[%s] SurgeGate OPEN — spike %.1fx baseline",
                watchdog_name, activity_ctx.spike_factor,
            )
            return True

        # Condition 3: any message in batch matches a broad keyword pattern
        if self._pattern is not None:
            for msg in messages:
                if msg.text and self._pattern.search(msg.text):
                    logger.debug(
                        "[%s] SurgeGate OPEN — keyword match in: %.60r",
                        watchdog_name, msg.text,
                    )
                    return True

        logger.debug(
            "[%s] SurgeGate CLOSED — rate=%.1f spike=%.1fx, skipping LLM",
            watchdog_name,
            activity_ctx.current_rate,
            activity_ctx.spike_factor,
        )
        return False
