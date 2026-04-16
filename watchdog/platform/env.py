"""
SecretsProvider implementation: environment variables / .env file.

Used for local development where secrets are already in os.environ
(loaded by python-dotenv before this provider is called).

This is a no-op provider: get_secret() always returns None, relying on
load_secrets() to skip env vars that are already set.  The effect is that
dotenv-loaded values are preserved unchanged.
"""
from __future__ import annotations

from watchdog.platform.base import SecretsProvider


class EnvSecretsProvider(SecretsProvider):
    """
    No-op secrets provider for local .env usage.

    Secrets are already in os.environ via load_dotenv() — this provider
    does nothing, letting load_secrets() leave existing env vars untouched.

    Also useful as a base for testing: subclass and override get_secret()
    to inject specific values without touching os.environ globally.
    """

    @property
    def provider_name(self) -> str:
        return "EnvSecretsProvider(.env)"

    def get_secret(self, name: str) -> str | None:
        return None
