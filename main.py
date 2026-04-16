"""
Message Watchdog — entry point.

Usage
-----
  python main.py                 # run all watchdogs defined in config.yaml
  python main.py --setup         # first-run Telegram authentication wizard
  python main.py --dry-run       # run but log alerts instead of sending them
  python main.py --config path   # use a different config file

How it works
------------
1. config.yaml is loaded → list[WatchdogConfig]
2. Watchdogs are grouped by (source_type, source_group) so a single
   source connection is shared by all watchdogs targeting the same group.
3. The LLM provider is built from config.yaml (llm.provider field).
4. Alert channel instances are built for each watchdog.
5. TelegramSelfAlert reuses the TelegramSource client to avoid session locking.
6. All engine.run() coroutines + source.start() run concurrently.
7. SIGINT (Ctrl-C) triggers graceful shutdown.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from watchdog.core.classifier import LLMClassifier
from watchdog.core.engine import WatchdogEngine
from watchdog.core.models import SurgeGateConfig, WatchdogConfig
from watchdog.core.providers.base import LLMProvider
from watchdog.sources.telegram import TelegramSource


# ---------------------------------------------------------------------------
# LLM provider factory
# ---------------------------------------------------------------------------

def _build_llm_provider(cfg: dict) -> LLMProvider:
    """
    Build the LLM provider from the `llm:` section of config.yaml.

    Supported providers:
      anthropic   — Anthropic Claude (default)
      azure_openai — Azure OpenAI Service or Azure AI Foundry endpoint
    """
    llm_cfg = cfg.get("llm", {})
    provider_name = llm_cfg.get("provider", "anthropic")

    if provider_name == "anthropic":
        from watchdog.core.providers.anthropic import AnthropicProvider
        return AnthropicProvider(
            api_key=_require_env("ANTHROPIC_API_KEY"),
            primary_model=llm_cfg.get("primary_model", "claude-haiku-4-5-20251001"),
            escalation_model=llm_cfg.get("escalation_model", "claude-sonnet-4-6"),
        )

    if provider_name == "azure_openai":
        from watchdog.core.providers.azure_openai import AzureOpenAIProvider
        return AzureOpenAIProvider(
            endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
            api_key=_require_env("AZURE_OPENAI_API_KEY"),
            deployment=llm_cfg.get("azure_deployment", "gpt-4o-mini"),
            api_version=llm_cfg.get("azure_api_version", "2024-02-01"),
        )

    logging.critical(
        "Unknown LLM provider '%s'. Supported: anthropic, azure_openai", provider_name
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Alert channel factory
# ---------------------------------------------------------------------------

def _build_alert_channels(config: WatchdogConfig, cfg: dict, source_map: dict):
    channels = []
    channel_cfg = cfg.get("alert_channels", {})

    for channel_name in config.alert_channels:
        channel = None

        if channel_name == "phone_call":
            if not channel_cfg.get("phone_call", {}).get("enabled", True):
                continue
            from watchdog.alerts.phone_call import TwilioCallAlert
            channel = TwilioCallAlert(
                account_sid=_require_env("TWILIO_ACCOUNT_SID"),
                auth_token=_require_env("TWILIO_AUTH_TOKEN"),
                from_number=_require_env("TWILIO_FROM_NUMBER"),
                to_number=_require_env("TWILIO_TO_NUMBER"),
                watchdog_name=config.name,
            )

        elif channel_name == "telegram_self":
            if not channel_cfg.get("telegram_self", {}).get("enabled", True):
                continue
            from watchdog.alerts.telegram_self import TelegramSelfAlert
            source_key = ("telegram", config.source_group)
            source = source_map.get(source_key)
            if source is None:
                logging.warning(
                    "telegram_self alert for '%s' skipped — no TelegramSource found",
                    config.name,
                )
                continue
            channel = TelegramSelfAlert(client=source.client, watchdog_name=config.name)

        elif channel_name == "email":
            if not channel_cfg.get("email", {}).get("enabled", True):
                continue
            from watchdog.alerts.email_alert import GmailAlert
            channel = GmailAlert(
                gmail_address=_require_env("GMAIL_SENDER"),
                app_password=_require_env("GMAIL_APP_PASSWORD"),
                to_address=_require_env("GMAIL_RECIPIENT"),
                watchdog_name=config.name,
            )

        else:
            logging.warning("Unknown alert channel '%s' — skipping", channel_name)

        if channel is not None:
            channel.config_name = channel_name
            channels.append(channel)

    return channels


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_watchdog_configs(cfg: dict, global_dry_run: bool) -> list[WatchdogConfig]:
    configs = []
    for entry in cfg.get("watchdogs", []):
        sg_raw = entry.get("surge_gate", {})
        surge_gate = SurgeGateConfig(
            enabled=sg_raw.get("enabled", True),
            min_spike_factor=float(sg_raw.get("min_spike_factor", 2.5)),
            always_classify_rate=float(sg_raw.get("always_classify_rate", 3.0)),
            keyword_patterns=sg_raw.get(
                "keyword_patterns", [r"h\s*[-]?\s*1\s*b"]
            ),
        )
        configs.append(
            WatchdogConfig(
                name=entry["name"],
                source_type=entry.get("source_type", "telegram"),
                source_group=entry["source_group"],
                condition=entry["condition"],
                confidence_threshold=float(entry.get("confidence_threshold", 0.75)),
                alert_channels=entry.get("alert_channels", ["telegram_self"]),
                cooldown_seconds=int(entry.get("cooldown_seconds", 300)),
                batch_window_seconds=int(entry.get("batch_window_seconds", 60)),
                batch_burst_cap=int(entry.get("batch_burst_cap", 30)),
                dry_run=global_dry_run or bool(cfg.get("dry_run", False)),
                surge_gate=surge_gate,
                channel_thresholds={
                    k: float(v)
                    for k, v in entry.get("channel_thresholds", {}).items()
                },
            )
        )
    if not configs:
        logging.critical("No watchdogs defined in config.yaml. Exiting.")
        sys.exit(1)
    return configs


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        logging.critical("Required environment variable '%s' is not set.", key)
        sys.exit(1)
    return val


def _setup_logging(log_dir: str, level: str) -> None:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "watchdog.log"
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ]
    logging.basicConfig(level=numeric_level, format=fmt, handlers=handlers)


# ---------------------------------------------------------------------------
# Main async runner
# ---------------------------------------------------------------------------

async def _run(cfg: dict, watchdog_configs: list[WatchdogConfig]) -> None:
    telegram_cfg = cfg.get("sources", {}).get("telegram", {})
    session_name = telegram_cfg.get("session_name", "watchdog_session")

    api_id = int(_require_env("TELEGRAM_API_ID"))
    api_hash = _require_env("TELEGRAM_API_HASH")
    phone = _require_env("TELEGRAM_PHONE")

    # Build LLM provider (single shared instance across all watchdog engines)
    provider = _build_llm_provider(cfg)
    classifier = LLMClassifier(provider)
    logging.info("LLM provider: %s", provider.provider_name)

    # Build one TelegramSource per unique group
    source_map: dict[tuple[str, str], TelegramSource] = {}
    for wc in watchdog_configs:
        key = (wc.source_type, wc.source_group)
        if key not in source_map and wc.source_type == "telegram":
            source_map[key] = TelegramSource(
                api_id=api_id,
                api_hash=api_hash,
                phone=phone,
                session_name=session_name,
                group=wc.source_group,
                startup_lookback_minutes=telegram_cfg.get("startup_lookback_minutes", 10),
            )

    # Build engines and wire them to their source
    engines: list[WatchdogEngine] = []
    for wc in watchdog_configs:
        channels = _build_alert_channels(wc, cfg, source_map)
        engine = WatchdogEngine(config=wc, classifier=classifier, alert_channels=channels)
        engines.append(engine)
        key = (wc.source_type, wc.source_group)
        source = source_map.get(key)
        if source:
            source.register(engine.queue.put)

    tasks = [asyncio.create_task(engine.run()) for engine in engines]
    tasks += [asyncio.create_task(source.start()) for source in source_map.values()]

    logging.info(
        "Watchdog started — %d watchdog(s), %d source(s)", len(engines), len(source_map)
    )

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        for engine in engines:
            engine.stop()
        for source in source_map.values():
            await source.stop()
        logging.info("Watchdog stopped. LLM stats: %s", classifier.stats)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Message Watchdog — LLM-powered group chat monitor"
    )
    parser.add_argument("--setup", action="store_true",
                        help="Run the first-time Telegram authentication wizard")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log alerts instead of sending them (safe for testing)")
    parser.add_argument("--config", default="config.yaml", metavar="PATH",
                        help="Path to the config file (default: config.yaml)")
    args = parser.parse_args()

    load_dotenv()

    # Select the secrets provider based on environment.
    # Azure Key Vault is used in production (AZURE_KEY_VAULT_URL set by deploy.yml).
    # EnvSecretsProvider is the no-op fallback for local .env usage.
    from watchdog.platform.base import load_secrets
    vault_url = os.getenv("AZURE_KEY_VAULT_URL")
    if vault_url:
        from watchdog.platform.azure_keyvault import AzureKeyVaultProvider
        secrets_provider = AzureKeyVaultProvider(vault_url)
    else:
        from watchdog.platform.env import EnvSecretsProvider
        secrets_provider = EnvSecretsProvider()
    load_secrets(secrets_provider)

    if args.setup:
        from setup import run_setup
        run_setup()
        return

    if not Path(args.config).exists():
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    _setup_logging(cfg.get("log_dir", "logs"), cfg.get("log_level", "INFO"))

    if args.dry_run:
        logging.warning("DRY RUN mode — alerts will be logged but NOT sent")

    watchdog_configs = _load_watchdog_configs(cfg, args.dry_run)

    try:
        asyncio.run(_run(cfg, watchdog_configs))
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")


if __name__ == "__main__":
    main()
