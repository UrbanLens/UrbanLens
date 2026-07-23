"""WhatsApp notification gateway, via Twilio's Messaging API for WhatsApp."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from urbanlens.dashboard.services.apis.messaging.base import TwilioGateway
from urbanlens.UrbanLens.settings.app import settings


@dataclass(slots=True, kw_only=True)
class WhatsAppGateway(TwilioGateway):
    service_key: ClassVar[str] = "whatsapp"

    account_sid: str | None = settings.twilio_account_sid
    auth_token: str | None = settings.twilio_auth_token
    from_number: str | None = settings.twilio_whatsapp_from_number

    def _address(self, number: str) -> str:
        """Twilio's WhatsApp API addresses numbers with a ``whatsapp:`` prefix."""
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"
