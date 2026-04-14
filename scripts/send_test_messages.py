"""
Send test messages to the test group to exercise the classifier.
Run in a second terminal while main.py is running.
"""
from __future__ import annotations

import asyncio
import os
import yaml
from dotenv import load_dotenv

TEST_GROUP = "https://t.me/+wHiZiSj6OBAxYTZh"

MESSAGES = [
    "Anything in Chennai?",
    "yes, alot of them in Hyderabad",
    "Bulk slots are only open in Hyderbad, for both VAC and Consular.",
    "Got the H4",
    "H4 Hyderabad still available for October and November",
    "Able to book for oct 22 and oct 27 in hyderabad",
    "Just got the slot for Chennai ofc Sept and hyd consular October",
    "Got in hyd h1 and h4 oct dates",
    "Booked for Hyd Nov 5th and Nov 10th",
    "Got appointment H1 and H4 - Nov 3 OFC, Nov 24 consular",
    "Got appointment for October",
    "Hyderabad H4 November available",
    "H1 and H4 slots open HYD!! Go now",
    "I got Nov just be patient it keeps kicking out but stay on it",
    "Slots gone now for HYD, anyone see Chennai?",
    "No slots available for H1",
    "All NA now",
    "Finally got a slot! 2nov & 9nov Hyd",
    "Booked for November. Got email confirmation.",
    "No more slots across all locations",
    "Next friday?",
    "This Friday and last Friday around 6 pm IST",
    "Slots opened at around 7PM IST",
]

DELAY_SECONDS = 2


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
        session_name = cfg.get("sources", {}).get("telegram", {}).get("session_name", session_name)
    except Exception:
        pass

    async with TelegramClient(session_name, api_id, api_hash) as client:
        await client.start(phone=lambda: phone)
        entity = await client.get_entity(TEST_GROUP)
        print(f"Sending {len(MESSAGES)} messages to '{getattr(entity, 'title', TEST_GROUP)}'...\n")

        for i, msg in enumerate(MESSAGES, 1):
            await client.send_message(entity, msg)
            print(f"[{i}/{len(MESSAGES)}] {msg}")
            await asyncio.sleep(DELAY_SECONDS)

        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
