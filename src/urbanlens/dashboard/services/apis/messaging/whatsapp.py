"""WhatsApp notification gateway, via Twilio's Messaging API for WhatsApp."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from urbanlens.dashboard.services.apis.messaging.base import TwilioGateway
from urbanlens.UrbanLens.settings.app import settings


@dataclass(slots=True, kw_only=True)
class WhatsAppGateway(TwilioGateway):
    service_key: ClassVar[str] = "whatsapp"

    # default_factory, not a bare default: a dataclass field's bare default is evaluated
    # once at class-definition/import time, so a later settings change never reaches
    # subsequent instantiations - default_factory re-reads it fresh each time.
    account_sid: str | None = field(default_factory=lambda: settings.twilio_account_sid)
    auth_token: str | None = field(default_factory=lambda: settings.twilio_auth_token)
    from_number: str | None = field(default_factory=lambda: settings.twilio_whatsapp_from_number)

    def _address(self, number: str) -> str:
        """Twilio's WhatsApp API addresses numbers with a ``whatsapp:`` prefix."""
        return number if number.startswith("whatsapp:") else f"whatsapp:{number}"
