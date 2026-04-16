# Message Watchdog

A 24/7 message group monitor that uses an LLM to detect real-world events from noisy chat traffic and fires unstoppable alerts — phone call, Telegram message, and email.

Built to catch H1B visa stamping slot openings at US consulates in India, where slots appear in bulk with no notice and fill within minutes. The core engine is source-agnostic: Telegram is the first source implementation, but the architecture supports additional sources (WhatsApp, Slack, Discord, etc.) without changes to the detection or alerting logic.

---

## I want to run this locally

```powershell
git clone https://github.com/gvarun22/Message-Watchdog.git
cd Message-Watchdog
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux
pip install -r requirements.txt
copy .env.example .env       # then fill in your credentials
python setup.py              # first-time source authentication (Telegram)
python main.py --dry-run     # run without sending alerts
python main.py               # run for real
```

---

## I want phone call alerts

Phone calls ring through Do Not Disturb on iOS and Android — the only alert channel that wakes you up.

1. Create a free Twilio account at [console.twilio.com](https://console.twilio.com) and get a phone number. The free trial gives ~$15 credit (~1,000 calls). After trial, US calls cost ~$0.013/min.

2. Add these to your `.env`:
   ```
   TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxx
   TWILIO_AUTH_TOKEN=your_token
   TWILIO_FROM_NUMBER=+1XXXXXXXXXX   # your Twilio number
   TWILIO_TO_NUMBER=+1XXXXXXXXXX     # your mobile
   ```

3. In `config.yaml`, add `phone_call` to `alert_channels` for your watchdog.

4. Add the Twilio number to your phone contacts (e.g. "H1B Alert"), then enable DND bypass:
   - **iPhone**: Settings → Focus → Do Not Disturb → People → Allow Calls From → add contact
   - **Android**: DND Settings → Exceptions → Calls from specific contacts → add contact

> **Twilio trial accounts** play a promotional message before your TwiML. Upgrade at console.twilio.com → Billing to remove it.

---

## I want email alerts

1. Generate a Gmail App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-Step Verification). This is a 16-character code — **not** your Gmail password.

2. Add these to your `.env`:
   ```
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   GMAIL_SENDER=you@gmail.com      # the account the app password belongs to
   GMAIL_RECIPIENT=you@gmail.com   # where to send alerts
   ```

3. In `config.yaml`, set `email.enabled: true` and add `email` to `alert_channels` for your watchdog.

---

## I want to monitor WhatsApp (or another source)

The source layer is a pluggable adapter. To add a new source:

1. Create `watchdog/sources/whatsapp.py` (or `slack.py`, `discord.py`, etc.) implementing the `MessageSource` ABC from `watchdog/sources/base.py`. Use `watchdog/sources/telegram.py` as a reference — the contract is:
   - `async def start(on_message)` — connect, register a handler that calls `on_message(Message)` for each new message
   - `async def stop()` — disconnect cleanly
   - `source_type: str` property — return e.g. `"whatsapp"`

2. Register the new source type in `main.py` alongside the existing `telegram` case.

3. In `config.yaml`, set `source_type: whatsapp` on any watchdog that should use it.

No changes to the engine, classifier, surge gate, or alert channels are needed.

---

## I want to use a different cloud provider (or self-host)

The secrets layer is a pluggable adapter, the same as sources and alerts. The `SecretsProvider` ABC lives in `watchdog/platform/base.py`:

| What to swap | Where | How |
|---|---|---|
| Secrets store | `watchdog/platform/` | Subclass `SecretsProvider`, implement `get_secret(name)`. See `azure_keyvault.py` as a reference. Wire it up in `main.py` based on whatever env var signals which backend is active. |
| Container host | Infrastructure only | Any Docker host works — AWS ECS/Fargate, GCP Cloud Run, fly.io, a plain VPS. No code changes. |
| Session file storage | Infrastructure only | Any mounted volume. The app reads/writes a plain file path — swap the Azure File Share for S3Fuse, a GCS mount, or local disk. |

**To run without any cloud:** skip `AZURE_KEY_VAULT_URL`, put all credentials in `.env`, and run `python main.py` or `docker run` anywhere. `EnvSecretsProvider` is used automatically and secrets are read from the environment.

---

## Configuration

All watchdog logic lives in `config.yaml` — no code changes needed to add, remove, or tune a watchdog.

```yaml
watchdogs:
  - name: "H1B Slot Opening India"
    source_type: telegram
    source_group: "https://t.me/your_group"
    condition: >
      Alert me when H1B visa stamping slots appear to be opening...
    confidence_threshold: 0.72   # lower = more sensitive; raise to reduce false positives
    alert_channels: [phone_call, telegram_self, email]
    cooldown_seconds: 300        # minimum gap between alerts for this watchdog
    batch_window_seconds: 60     # collect messages for this long before classifying
    batch_burst_cap: 30          # classify immediately if this many messages arrive
    surge_gate:
      enabled: true              # skip LLM when quiet — saves ~95% of token costs
      min_spike_factor: 2.5      # classify if rate >= 2.5x baseline
      always_classify_rate: 3.0  # always classify if rate >= 3 msg/min
      keyword_patterns:          # classify if any message matches
        - 'slot'
        - 'h1b'
    channel_thresholds:          # per-channel confidence overrides (optional)
      phone_call: 0.90           # only call for high-confidence events
      telegram_self: 0.80        # Telegram message at moderate confidence
                                 # email: falls back to confidence_threshold
```

**LLM providers** — switch between Anthropic Claude and Azure OpenAI by changing `llm.provider` in `config.yaml`. See `.env.example` for the credentials each provider needs.

---

## Azure deployment

The container runs on Azure Container Instances. Session files persist across restarts via an Azure File Share mount.

**One-time infrastructure setup:**

```powershell
# Windows (requires: az login --tenant <your-tenant>, gh auth login)
.\scripts\azure-setup.ps1 `
  -SubscriptionId "<your-subscription-id>" `
  -GitHubRepo "owner/Message-Watchdog"

# macOS / Linux
SUBSCRIPTION_ID="<your-subscription-id>" \
GITHUB_REPO="owner/Message-Watchdog" \
./scripts/azure-setup.sh
```

The script creates all required Azure resources, stores every credential in Key Vault, and sets the 7 GitHub Actions secrets. Nothing sensitive is stored in GitHub. The storage account name is auto-generated from the subscription ID (globally unique per account).

**Deploy:** push to `main`. GitHub Actions builds the Docker image, pushes it to ACR, and recreates the container. Pushes that only change `scripts/`, `*.md`, or `.claude/` are ignored — no redeploy triggered.

**Check live logs:**
```powershell
az container logs --name message-watchdog --resource-group Message-Watchdog --follow
```

---

## Project structure

```
config.yaml               — watchdog definitions (the main thing to edit)
main.py                   — entry point
setup.py                  — first-run source authentication wizard
watchdog/
  platform/
    base.py               — SecretsProvider ABC + load_secrets() (implement to add a new secrets backend)
    azure_keyvault.py     — Azure Key Vault implementation
    env.py                — no-op provider for local .env usage
  core/
    engine.py             — message buffering, batching, cooldown
    classifier.py         — LLM prompt construction and response parsing
    surge_gate.py         — keyword + rate-spike filter (cost guard)
    activity_tracker.py   — sliding-window message rate tracker
    models.py             — shared dataclasses
    providers/            — LLM provider adapters (Anthropic, Azure OpenAI)
  sources/
    base.py               — MessageSource ABC (implement to add a new source)
    telegram.py           — Telethon-based Telegram implementation
  alerts/
    base.py               — AlertChannel ABC (implement to add a new channel)
    phone_call.py         — Twilio voice call
    telegram_self.py      — Telegram self-message
    email_alert.py        — Gmail SMTP
scripts/
  azure-setup.ps1         — infrastructure provisioning (Windows)
  azure-setup.sh          — infrastructure provisioning (macOS / Linux)
```
