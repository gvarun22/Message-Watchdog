"""
LLM provider: Anthropic Claude.

Uses two models:
  Primary    : claude-haiku-4-5-20251001   fast, cheap  (~$0.000002/call)
  Escalation : claude-sonnet-4-6           used only for ambiguous cases

The escalation model is activated by the classifier layer when the primary
model returns triggered=True with 0.50 <= confidence < 0.75.
"""
from __future__ import annotations

import logging
import time

import anthropic as _anthropic

from watchdog.core.providers.base import LLMProvider
from watchdog.core.utils import run_sync

logger = logging.getLogger(__name__)

_DEFAULT_PRIMARY = "claude-haiku-4-5-20251001"
_DEFAULT_ESCALATION = "claude-sonnet-4-6"


class AnthropicProvider(LLMProvider):
    """Calls the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        primary_model: str = _DEFAULT_PRIMARY,
        escalation_model: str = _DEFAULT_ESCALATION,
    ) -> None:
        self._client = _anthropic.Anthropic(api_key=api_key)
        self._primary = primary_model
        self._escalation = escalation_model

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def primary_model(self) -> str:
        return self._primary

    @property
    def escalation_model(self) -> str:
        return self._escalation

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 256,
        model: str | None = None,
    ) -> tuple[str, int, str]:
        target_model = model or self._primary
        t0 = time.monotonic()

        try:
            response = await run_sync(
                self._client.messages.create,
                model=target_model,
                max_tokens=max_tokens,
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except _anthropic.APIError as exc:
            logger.error("[anthropic] API error (%s): %s", target_model, exc)
            raise

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        tokens = response.usage.input_tokens + response.usage.output_tokens
        text = response.content[0].text.strip()
        logger.debug("[anthropic] %s | tokens=%d latency=%dms", target_model, tokens, elapsed_ms)
        return text, tokens, target_model
