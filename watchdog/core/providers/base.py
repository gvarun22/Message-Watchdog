"""
Abstract base class for LLM providers.

To add a new provider (e.g. Google Gemini, local Ollama, Mistral API):
  1. Create a new module in watchdog/core/providers/
  2. Subclass LLMProvider and implement complete() and provider_name
  3. Register the provider name in main.py's PROVIDER_REGISTRY
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """
    Wraps a specific LLM API and exposes a single method: complete().

    The classifier layer handles prompt construction, JSON parsing, and
    the two-tier escalation logic. The provider only handles the HTTP call.
    """

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 256,
    ) -> tuple[str, int, str]:
        """
        Send a prompt to the LLM and return the response.

        Returns
        -------
        (response_text, tokens_used, model_name_or_deployment)
        """
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short human-readable name, e.g. 'anthropic', 'azure_openai'."""
        ...

    @property
    def escalation_model(self) -> str | None:
        """
        Return the name/ID of a stronger model to use when the primary model
        returns an ambiguous result, or None if this provider has only one
        deployment and escalation is not supported.
        """
        return None
