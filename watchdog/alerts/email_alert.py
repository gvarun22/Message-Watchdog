"""
Alert channel: Gmail email via SMTP.

Uses an App Password (not OAuth) for simplicity in container environments.
Generate one at: https://myaccount.google.com/apppasswords
(Requires 2-Step Verification on the Google account.)

smtplib is synchronous — the actual SMTP call runs in a thread executor
so it never blocks the asyncio event loop.
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from watchdog.alerts.base import AlertChannel
from watchdog.core.models import ClassificationResult
from watchdog.core.utils import run_sync

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 587   # STARTTLS


class GmailAlert(AlertChannel):
    """Sends the alert as an email via Gmail SMTP."""

    def __init__(
        self,
        gmail_address: str,
        app_password: str,
        to_address: str,
        watchdog_name: str,
    ) -> None:
        self._from = gmail_address
        self._password = app_password
        self._to = to_address
        self._watchdog_name = watchdog_name

    @property
    def channel_name(self) -> str:
        return f"Gmail→{self._to}"

    async def send(self, message: str, result: ClassificationResult) -> None:
        try:
            await run_sync(self._send_sync, message, result)
            logger.info("[%s] GmailAlert sent to %s", self._watchdog_name, self._to)
        except Exception as exc:
            logger.error("[%s] GmailAlert failed: %s", self._watchdog_name, exc)

    def _send_sync(self, message: str, result: ClassificationResult) -> None:
        subject = f"[WATCHDOG] {self._watchdog_name} — confidence {int(result.confidence * 100)}%"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = self._to

        plain_part = MIMEText(message, "plain", "utf-8")
        msg.attach(plain_part)

        with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(self._from, self._password)
            smtp.sendmail(self._from, self._to, msg.as_string())
