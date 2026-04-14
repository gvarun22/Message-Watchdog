"""
LLM-powered message batch classifier.

Constructs the prompt, calls the LLM provider, parses the JSON response,
and applies two-tier escalation (when the provider supports it).

Two-tier escalation (Anthropic only)
--------------------------------------
When the primary model (Haiku) returns triggered=True AND
0.50 <= confidence < 0.75, we re-run with the escalation model (Sonnet).
This keeps cost near-zero for clear cases while using stronger reasoning
for genuinely ambiguous batches.

For providers with a single deployment (e.g. Azure OpenAI), escalation is
skipped — choose a capable model like gpt-4o-mini in that case.
"""
from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from watchdog.core.models import ActivityContext, ClassificationResult, Message
from watchdog.core.providers.base import LLMProvider

if TYPE_CHECKING:
    from watchdog.core.providers.anthropic import AnthropicProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a message monitoring assistant. Your job is to analyze a batch of group chat messages and determine whether a specific user-defined condition has been met.

You MUST respond with valid JSON only — no markdown fences, no explanation outside the JSON object.

Output schema (strict):
{
  "triggered": <boolean>,
  "confidence": <float 0.0 to 1.0>,
  "reason": "<1-2 sentence explanation of your determination>",
  "key_signals": ["<specific message snippet or observation that influenced the decision>", ...]
}

Guidelines:
- Consider the batch AS A WHOLE — context across messages matters more than any single message in isolation
- Be robust to misspellings, abbreviations, slang, informal writing, emoji, and mixed languages
- Media messages (labelled [sent photo], [sent video], etc.) in a burst often indicate excitement about a real-world event
- Message rate spikes (high spike_factor in the activity context) are meaningful signals — sudden bursts accompany events
- Err on the side of catching real events: false positives are preferable to false negatives
- confidence should reflect your certainty that the CONDITION IS MET, not just that the general topic is mentioned
- key_signals should be 1-4 short, specific excerpts or observations — not summaries"""

USER_PROMPT_TEMPLATE = """\
## Watchdog Condition
{condition}

## Activity Context
Current message rate : {current_rate:.1f} msg/min
Baseline rate        : {baseline_rate:.1f} msg/min
Spike factor         : {spike_factor:.1f}x above baseline
Window               : last {window_seconds} seconds

## Message Batch  ({count} messages from "{chat_name}")
{messages_block}

---
Analyze this batch against the watchdog condition above. Respond with JSON only."""

# Ambiguous confidence range — triggers escalation to stronger model
_AMBIGUOUS_LOW = 0.50
_AMBIGUOUS_HIGH = 0.75


def _format_messages_block(messages: list[Message]) -> str:
    return "\n".join(m.format_for_log(indent="") for m in messages)


def _parse_response(raw_text: str, model: str, tokens: int, latency_ms: int) -> ClassificationResult:
    """Parse the LLM JSON response into a ClassificationResult."""
    text = raw_text.strip()
    # Claude/GPT occasionally wrap JSON in markdown fences despite instructions
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
        return ClassificationResult(
            triggered=bool(data.get("triggered", False)),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
            key_signals=list(data.get("key_signals", [])),
            model_used=model,
            tokens_used=tokens,
            latency_ms=latency_ms,
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error("Failed to parse LLM response as JSON: %s\nRaw: %s", exc, raw_text)
        return ClassificationResult(
            triggered=False,
            confidence=0.0,
            reason=f"JSON parse error: {exc}",
            key_signals=[],
            model_used=model,
            tokens_used=tokens,
            latency_ms=latency_ms,
        )


class LLMClassifier:
    """
    Classifies message batches against a natural-language condition.
    Provider-agnostic — works with Anthropic, Azure OpenAI, or any LLMProvider.
    """

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self._total_calls = 0
        self._total_tokens = 0

    async def classify(
        self,
        messages: list[Message],
        condition: str,
        activity_context: ActivityContext,
        chat_name: str,
    ) -> ClassificationResult:
        """
        Classify a batch of messages against the given condition.
        Never raises — API errors are returned as triggered=False.
        """
        if not messages:
            return ClassificationResult(
                triggered=False,
                confidence=0.0,
                reason="Empty message batch — nothing to classify.",
                key_signals=[],
                model_used=self._provider.provider_name,
                tokens_used=0,
                latency_ms=0,
            )

        user_prompt = USER_PROMPT_TEMPLATE.format(
            condition=condition,
            current_rate=activity_context.current_rate,
            baseline_rate=activity_context.baseline_rate,
            spike_factor=activity_context.spike_factor,
            window_seconds=activity_context.window_seconds,
            count=len(messages),
            chat_name=chat_name,
            messages_block=_format_messages_block(messages),
        )

        result = await self._call(user_prompt)

        # Two-tier escalation — only when provider has a stronger model configured
        if (
            result.triggered
            and self._provider.escalation_model is not None
            and _AMBIGUOUS_LOW <= result.confidence < _AMBIGUOUS_HIGH
        ):
            logger.info(
                "[%s] Ambiguous confidence %.2f from primary — escalating",
                chat_name, result.confidence,
            )
            result = await self._call(user_prompt, escalate=True)

        self._total_calls += 1
        self._total_tokens += result.tokens_used
        logger.debug(
            "Classification | triggered=%s confidence=%.2f model=%s tokens=%d latency=%dms",
            result.triggered, result.confidence,
            result.model_used, result.tokens_used, result.latency_ms,
        )
        return result

    async def _call(self, user_prompt: str, *, escalate: bool = False) -> ClassificationResult:
        """Single LLM call with error handling."""
        t0 = time.monotonic()
        try:
            model_override = self._provider.escalation_model if escalate else None
            raw, tokens, model = await self._provider.complete(
                SYSTEM_PROMPT, user_prompt,
                **({"model": model_override} if model_override else {}),
            )

            latency = int((time.monotonic() - t0) * 1000)
            return _parse_response(raw, model, tokens, latency)

        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            logger.error("LLM call failed: %s", exc)
            return ClassificationResult(
                triggered=False,
                confidence=0.0,
                reason=f"LLM error: {exc}",
                key_signals=[],
                model_used=self._provider.provider_name,
                tokens_used=0,
                latency_ms=latency,
            )

    @property
    def stats(self) -> dict:
        return {
            "provider": self._provider.provider_name,
            "total_calls": self._total_calls,
            "total_tokens": self._total_tokens,
        }
