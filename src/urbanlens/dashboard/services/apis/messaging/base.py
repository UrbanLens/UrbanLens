"""Shared Twilio Messaging API plumbing for the SMS and WhatsApp gateways."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

import requests

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"


@dataclass(slots=True, kw_only=True)
class TwilioGateway(Gateway):
    """Base gateway for Twilio's Messaging API (shared by SMS and WhatsApp).

    Subclasses provide the ``from_number`` (and, for WhatsApp, the
    ``whatsapp:`` address prefix) - the HTTP call and auth are identical.
    """

    paid_service: ClassVar[bool] = True

    account_sid: str | None = None
    auth_token: str | None = None
    from_number: str | None = None

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.account_sid or not self.auth_token or not self.from_number:
            raise ValueError(f"{type(self).__name__} requires an account SID, auth token, and from-number to be configured.")

    def _address(self, number: str) -> str:
        """Format a bare E.164 number for this channel (overridden for WhatsApp)."""
        return number

    def send(self, to_number: str, body: str) -> bool:
        """Send a text message, returning whether Twilio accepted it.

        Args:
            to_number: Destination phone number, in E.164 format (e.g. ``+15551234567``).
            body: Message text.

        Returns:
            True if Twilio accepted the message for delivery, False on failure
            (logged, never raised - a failed notification shouldn't break the
            caller's own request/task).
        """
        # Narrowed to local variables (rather than trusting __post_init__'s check
        # of the instance attributes) so the type checker can see these are
        # non-None at the point of use.
        account_sid, auth_token, from_number = self.account_sid, self.auth_token, self.from_number
        if not account_sid or not auth_token or not from_number:
            raise ValueError(f"{type(self).__name__} is not configured.")

        url = _MESSAGES_URL.format(account_sid=account_sid)
        data = {
            "To": self._address(to_number),
            "From": self._address(from_number),
            "Body": body,
        }
        try:
            response = self.session.post(url, data=data, auth=(account_sid, auth_token), timeout=30)
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("Failed to send %s message via Twilio", type(self).service_key)
            return False
        return True
