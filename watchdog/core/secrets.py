"""
Azure Key Vault secret loader.

Runs at startup when AZURE_KEY_VAULT_URL is set. Fetches all mapped secrets
from Key Vault and injects them into os.environ so the rest of the app reads
them normally via os.getenv() / _require_env().

Uses DefaultAzureCredential, which resolves in order:
  1. Managed Identity        — Azure Container Instances (production)
  2. Azure CLI               — local development after `az login`
  3. VS Code / IntelliJ / workload identity credentials

When AZURE_KEY_VAULT_URL is not set (local .env mode) this module does nothing.
The caller is expected to have already run load_dotenv() before calling here.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Key Vault secret name  →  environment variable name.
# Only the secrets this application actually needs are listed.
# Secrets absent from Key Vault (e.g., Azure OpenAI when using Anthropic) are
# silently skipped so one Key Vault can serve multiple deployment configurations.
_SECRET_MAP: dict[str, str] = {
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
}


def load_secrets_from_key_vault() -> None:
    """
    Fetch secrets from Azure Key Vault and populate os.environ.

    - No-op when AZURE_KEY_VAULT_URL is unset.
    - Never raises — errors are logged and the process continues (it will fail
      later at _require_env() with a clear message if a required var is missing).
    - Env vars already present in os.environ are left untouched; a local .env
      file takes precedence over Key Vault for easy local overrides.
    """
    vault_url = os.getenv("AZURE_KEY_VAULT_URL")
    if not vault_url:
        return

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError:
        logger.error(
            "AZURE_KEY_VAULT_URL is set but azure-identity / azure-keyvault-secrets "
            "are not installed. Run: pip install azure-identity azure-keyvault-secrets"
        )
        return

    try:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_url, credential=credential)
    except Exception as exc:
        logger.error("Failed to create Key Vault client for %s: %s", vault_url, exc)
        return

    loaded = 0
    skipped = 0
    for secret_name, env_var in _SECRET_MAP.items():
        if os.getenv(env_var):
            # Already populated (e.g., from .env) — local override wins.
            skipped += 1
            continue
        try:
            secret = client.get_secret(secret_name)
            os.environ[env_var] = secret.value or ""
            loaded += 1
        except Exception as exc:
            # Absent secrets are normal — not every deployment needs all credentials.
            logger.debug("Key Vault: secret '%s' not loaded: %s", secret_name, exc)

    logger.info(
        "Key Vault: loaded %d secret(s) from %s (%d already in env, %d absent/skipped)",
        loaded,
        vault_url,
        skipped,
        len(_SECRET_MAP) - loaded - skipped,
    )
