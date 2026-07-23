"""SMS notification gateway, via Twilio's Messaging API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from urbanlens.dashboard.services.apis.messaging.base import TwilioGateway
from urbanlens.UrbanLens.settings.app import settings


@dataclass(slots=True, kw_only=True)
class SmsGateway(TwilioGateway):
    service_key: ClassVar[str] = "sms"

    account_sid: str | None = settings.twilio_account_sid
    auth_token: str | None = settings.twilio_auth_token
    from_number: str | None = settings.twilio_sms_from_number
