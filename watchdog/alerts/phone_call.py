"""
Alert channel: Twilio voice call.

This is the PRIMARY alert channel because a phone call rings through
Do Not Disturb / silent mode on both iOS and Android when the caller
is added to the contacts with DND bypass enabled.

Setup note for the user
-----------------------
Add the TWILIO_FROM_NUMBER to your phone contacts as e.g. "H1B Alert".
  iOS    : Settings → Focus → Do Not Disturb → People → Allow Calls From → add contact
  Android: DND Settings → Exceptions → Calls from specific contacts → add contact
"""
from __future__ import annotations

import logging

from twilio.rest import Client as TwilioClient

from watchdog.alerts.base import AlertChannel
from watchdog.core.models import ClassificationResult
from watchdog.core.utils import run_sync

logger = logging.getLogger(__name__)

# TwiML spoken by Amazon Polly (Joanna voice).
# The reason is repeated to give the listener time to absorb it.
_TWIML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna" rate="90%">
    Check visa slots now. Check visa slots now.
    {reason}
    Confidence: {confidence_pct} percent.
  </Say>
  <Pause length="1"/>
  <Say voice="Polly.Joanna" rate="90%">
    Check visa slots now. {reason}
  </Say>
</Response>"""


class TwilioCallAlert(AlertChannel):
    """Places a Twilio voice call when an alert fires."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        from_number: str,
        to_number: str,
        watchdog_name: str,
    ) -> None:
        self._client = TwilioClient(account_sid, auth_token)
        self._from = from_number
        self._to = to_number
        self._watchdog_name = watchdog_name

    @property
    def channel_name(self) -> str:
        return f"TwilioCall→{self._to}"

    async def send(self, message: str, result: ClassificationResult) -> None:
        twiml = _TWIML_TEMPLATE.format(
            watchdog_name=self._watchdog_name,
            reason=result.reason[:300],   # keep TTS reasonable length
            confidence_pct=int(result.confidence * 100),
        )
        try:
            call = await run_sync(
                self._client.calls.create,
                twiml=twiml,
                to=self._to,
                from_=self._from,
            )
            logger.info(
                "[%s] Twilio call initiated — SID=%s to=%s",
                self._watchdog_name,
                call.sid,
                self._to,
            )
        except Exception as exc:
            logger.error("[%s] TwilioCallAlert failed: %s", self._watchdog_name, exc)
