"""
First-run interactive setup wizard.

Run with:  python main.py --setup
           (or directly: python setup.py)

This script:
  1. Validates that all required .env variables are present
  2. Authenticates with Telegram via the MTProto OTP flow
  3. Saves the session to <session_name>.session

The session file lets subsequent runs skip authentication entirely.
Keep it out of version control — it is equivalent to a login token.
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv


async def _do_auth(api_id: int, api_hash: str, phone: str, session_name: str) -> None:
    # Import here so setup.py can be imported even before telethon is installed
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    client = TelegramClient(session_name, api_id, api_hash)
    await client.connect()

    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(
                f"\nAlready authenticated as: {me.first_name} "
                f"(@{me.username or 'no username'})"
            )
            print(f"Session file: {session_name}.session")
            return

        print(f"\nSending OTP to {phone}…")
        await client.send_code_request(phone)

        otp = input("Enter the OTP you received in Telegram: ").strip()

        try:
            await client.sign_in(phone, otp)
        except SessionPasswordNeededError:
            password = input(
                "Two-factor authentication is enabled.\n"
                "Enter your Telegram 2FA password: "
            )
            await client.sign_in(password=password)

        me = await client.get_me()
        print(
            f"\nSuccess! Authenticated as: {me.first_name} "
            f"(@{me.username or 'no username'})"
        )
        print(f"Session saved to: {session_name}.session")
        print("\nYou can now run the watchdog with:  python main.py")
    finally:
        await client.disconnect()


def run_setup() -> None:
    load_dotenv()

    api_id_raw = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    phone = os.getenv("TELEGRAM_PHONE")

    missing = [
        name
        for name, val in [
            ("TELEGRAM_API_ID", api_id_raw),
            ("TELEGRAM_API_HASH", api_hash),
            ("TELEGRAM_PHONE", phone),
        ]
        if not val
    ]

    if missing:
        print("ERROR: Missing required .env variables:")
        for name in missing:
            print(f"  {name}")
        print("\nCopy .env.example to .env and fill in the values, then re-run setup.")
        sys.exit(1)

    # Load session name from config if available, fall back to default
    session_name = "watchdog_session"
    try:
        import yaml  # type: ignore[import]
        with open("config.yaml") as f:
            cfg = yaml.safe_load(f)
        session_name = cfg.get("sources", {}).get("telegram", {}).get(
            "session_name", session_name
        )
    except (FileNotFoundError, Exception):
        pass

    print("=" * 55)
    print("  Message Watchdog — First-time Setup")
    print("=" * 55)
    print(f"  Telegram phone : {phone}")
    print(f"  Session name   : {session_name}")
    print()

    asyncio.run(_do_auth(int(api_id_raw), api_hash, phone, session_name))  # type: ignore[arg-type]


if __name__ == "__main__":
    run_setup()
