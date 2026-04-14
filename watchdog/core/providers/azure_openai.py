"""
LLM provider: Azure OpenAI (or any Azure-hosted model with an OpenAI-compatible API).

This covers two Azure deployment options:

Option A — Azure OpenAI Service
---------------------------------
Deploy GPT-4o, GPT-4o-mini, or GPT-3.5-turbo via Azure OpenAI Service.
Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY in .env.
Set llm.azure_deployment in config.yaml to your deployment name.

Steps to deploy:
  1. Azure Portal → Create resource → "Azure OpenAI"
  2. After creation → Go to Azure OpenAI Studio → Deployments → New deployment
  3. Choose a model (gpt-4o-mini recommended: cheap + accurate)
  4. Copy the endpoint URL and API key from "Keys and Endpoint"

Option B — Azure AI Foundry (custom or open-source models)
------------------------------------------------------------
Deploy Phi-4, Mistral, Llama, or any ONNX model via Azure AI Foundry
managed online endpoints. These expose an OpenAI-compatible chat/completions
endpoint that works with this same provider.

Steps:
  1. Azure Portal → Azure AI Foundry → Models + endpoints → Deploy model
  2. Choose "Serverless API" for pay-per-token (cheapest for low-volume use)
  3. Copy the endpoint URL and the API key shown after deployment

Either way, the env variables are the same:
  AZURE_OPENAI_ENDPOINT   e.g. https://your-resource.openai.azure.com/
  AZURE_OPENAI_API_KEY    your key
  AZURE_OPENAI_API_VERSION e.g. 2024-02-01 (for Azure OpenAI Service)
                              or omit for AI Foundry serverless endpoints

Note: Azure costs come from your subscription / dev credits — no separate
billing for API calls if you're using Azure OpenAI with a dev subscription.
"""
from __future__ import annotations

import logging
import time

from openai import AzureOpenAI

from watchdog.core.providers.base import LLMProvider
from watchdog.core.utils import run_sync

logger = logging.getLogger(__name__)


class AzureOpenAIProvider(LLMProvider):
    """
    Calls an Azure-hosted OpenAI-compatible endpoint.
    Single deployment — no escalation (use a capable model like gpt-4o-mini).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-02-01",
    ) -> None:
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        self._deployment = deployment

    @property
    def provider_name(self) -> str:
        return "azure_openai"

    async def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 256,
        model: str | None = None,  # ignored — single deployment
    ) -> tuple[str, int, str]:
        t0 = time.monotonic()

        try:
            response = await run_sync(
                self._client.chat.completions.create,
                model=self._deployment,
                max_tokens=max_tokens,
                temperature=0.0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            logger.error("[azure_openai] API error (%s): %s", self._deployment, exc)
            raise

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        usage = response.usage
        tokens = (usage.prompt_tokens + usage.completion_tokens) if usage else 0
        text = response.choices[0].message.content.strip()

        logger.debug(
            "[azure_openai] %s | tokens=%d latency=%dms",
            self._deployment, tokens, elapsed_ms,
        )
        return text, tokens, self._deployment
