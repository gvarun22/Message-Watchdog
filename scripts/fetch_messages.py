"""
Fetch messages from a Telegram group within a specific time window.
Useful for pulling real historical messages to use in test scenarios.

Usage:
    python scripts/fetch_messages.py

Output: formatted messages printed to stdout, oldest first.
Copy the ones you want into your test group to exercise the classifier.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import yaml
from dotenv import load_dotenv
import os

# ---------------------------------------------------------------------------
# Configure these for the fetch
# ---------------------------------------------------------------------------
GROUP = "https://t.me/us_visa_stamping_india"

# 4 AM – 10 AM PDT on Thursday 2026-04-10
# PDT = UTC-7  →  add 7h for UTC
WINDOW_START = datetime(2026, 4, 10, 11, 0, 0, tzinfo=timezone.utc)   # 04:00 PDT / 16:30 IST
WINDOW_END   = datetime(2026, 4, 10, 17, 0, 0, tzinfo=timezone.utc)   # 10:00 PDT / 22:30 IST

MAX_FETCH = 500   # fetch up to this many messages (walks back from WINDOW_END)
# ---------------------------------------------------------------------------


async def main() -> None:
    load_dotenv()

    from telethon import TelegramClient

    api_id   = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    phone    = os.environ["TELEGRAM_PHONE"]

    session_name = "watchdog_session"
    try:
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        session_name = (
            cfg.get("sources", {}).get("telegram", {}).get("session_name", session_name)
        )
    except Exception:
        pass

    async with TelegramClient(session_name, api_id, api_hash) as client:
        await client.start(phone=lambda: phone)

        entity = await client.get_entity(GROUP)
        print(f"Fetching messages from: {getattr(entity, 'title', GROUP)}")
        print(f"Window: {WINDOW_START.strftime('%Y-%m-%d %H:%M UTC')} → "
              f"{WINDOW_END.strftime('%H:%M UTC')}  (17:00–19:00 IST)\n")

        # Fetch newest-first starting from WINDOW_END, stop when we pass WINDOW_START
        messages = []
        async for msg in client.iter_messages(entity, limit=MAX_FETCH, offset_date=WINDOW_END):
            ts = msg.date
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < WINDOW_START:
                break
            messages.append((ts, msg))

        messages.reverse()  # display oldest-first

        if not messages:
            print("No messages found in this window.")
            return

        print(f"Found {len(messages)} message(s):\n{'─' * 60}")
        for ts, msg in messages:
            time_ist = ts.strftime("%H:%M:%S")
            sender = "unknown"
            try:
                s = msg.sender
                if s:
                    parts = [
                        getattr(s, "first_name", None) or "",
                        getattr(s, "last_name", None) or "",
                    ]
                    sender = " ".join(p for p in parts if p).strip() or getattr(s, "username", "unknown")
            except Exception:
                pass

            content = msg.text or ""
            if msg.media and not content:
                content = "[media]"
            elif msg.media:
                content += "  [+media]"

            print(f"[{time_ist} IST] {sender}: {content}")

        print(f"{'─' * 60}\nTotal: {len(messages)} messages")


if __name__ == "__main__":
    asyncio.run(main())
