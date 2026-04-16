"""
SecretsProvider implementation: Azure Key Vault.

Used in production (Azure Container Instances) where AZURE_KEY_VAULT_URL is
set and the container has a user-assigned managed identity with
Key Vault Secrets User role.

For local development, use EnvSecretsProvider instead (secrets come from .env).
"""
from __future__ import annotations

import logging

from watchdog.platform.base import SecretsProvider

logger = logging.getLogger(__name__)


class AzureKeyVaultProvider(SecretsProvider):
    """
    Fetches secrets from Azure Key Vault using DefaultAzureCredential.

    Credential resolution order (handled by the Azure SDK):
      1. Managed Identity  — Azure Container Instances (production)
      2. Azure CLI         — local development after `az login`
      3. VS Code / other   — IDE credential plugins
    """

    def __init__(self, vault_url: str) -> None:
        self._vault_url = vault_url
        self._client = self._build_client(vault_url)

    def _build_client(self, vault_url: str):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
            return SecretClient(
                vault_url=vault_url,
                credential=DefaultAzureCredential(),
            )
        except ImportError:
            logger.error(
                "azure-identity / azure-keyvault-secrets not installed. "
                "Run: pip install azure-identity azure-keyvault-secrets"
            )
            return None
        except Exception as exc:
            logger.error("Failed to create Key Vault client for %s: %s", vault_url, exc)
            return None

    @property
    def provider_name(self) -> str:
        return f"AzureKeyVault({self._vault_url})"

    def get_secret(self, name: str) -> str | None:
        if self._client is None:
            return None
        try:
            return self._client.get_secret(name).value or None
        except Exception as exc:
            logger.debug("Key Vault: secret '%s' not found: %s", name, exc)
            return None
