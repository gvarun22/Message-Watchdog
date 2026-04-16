"""
SecretsProvider — platform abstraction for runtime secret loading.

Separates WHERE secrets come from (Azure Key Vault, AWS Secrets Manager,
GCP Secret Manager, HashiCorp Vault, plain env vars, …) from the rest of
the application, which never needs to know.

To add a new secrets backend:
  1. Create watchdog/platform/<provider>.py
  2. Subclass SecretsProvider and implement get_secret()
  3. Instantiate it in main.py based on whatever env var or config flag
     signals which backend is in use.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Contract: the canonical secret names the application needs.
# Keys are the names used in the secrets store (e.g. Key Vault secret name,
# AWS Secrets Manager secret id).  Values are the env var names the rest of
# the app reads via os.getenv() / _require_env().
# ---------------------------------------------------------------------------
SECRET_MAP: dict[str, str] = {
    "telegram-api-id":       "TELEGRAM_API_ID",
    "telegram-api-hash":     "TELEGRAM_API_HASH",
    "telegram-phone":        "TELEGRAM_PHONE",
    "anthropic-api-key":     "ANTHROPIC_API_KEY",
    "azure-openai-endpoint": "AZURE_OPENAI_ENDPOINT",
    "azure-openai-api-key":  "AZURE_OPENAI_API_KEY",
    "twilio-account-sid":    "TWILIO_ACCOUNT_SID",
    "twilio-auth-token":     "TWILIO_AUTH_TOKEN",
    "twilio-from-number":    "TWILIO_FROM_NUMBER",
    "twilio-to-number":      "TWILIO_TO_NUMBER",
    "gmail-app-password":    "GMAIL_APP_PASSWORD",
    "gmail-sender":          "GMAIL_SENDER",
    "gmail-recipient":       "GMAIL_RECIPIENT",
}


class SecretsProvider(ABC):
    """
    Fetches a named secret from whatever backing store this provider wraps.

    Implementations must be safe to call at startup before the asyncio loop
    runs — they may be synchronous or async internally but must expose a
    synchronous get_secret() for simplicity.
    """

    @abstractmethod
    def get_secret(self, name: str) -> str | None:
        """
        Return the secret value for *name*, or None if absent.
        Must not raise — return None on any retrieval failure.
        """
        ...

    @property
    def provider_name(self) -> str:
        return self.__class__.__name__


def load_secrets(provider: SecretsProvider) -> None:
    """
    Iterate over SECRET_MAP, fetch each secret from *provider*, and inject it
    into os.environ.  Env vars already set (e.g. from a local .env file via
    dotenv) are left untouched — local overrides always win.

    Never raises: missing secrets are silently skipped here and will surface
    later as clear errors at _require_env() call-sites.
    """
    loaded = skipped = absent = 0
    for secret_name, env_var in SECRET_MAP.items():
        if os.getenv(env_var):
            skipped += 1
            continue
        value = provider.get_secret(secret_name)
        if value is not None:
            os.environ[env_var] = value
            loaded += 1
        else:
            absent += 1

    logger.info(
        "Secrets loaded via %s: %d loaded, %d already in env, %d absent",
        provider.provider_name, loaded, skipped, absent,
    )
