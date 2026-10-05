"""Shared Twilio Messaging API plumbing for the SMS and WhatsApp gateways."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import ClassVar

import requests

from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.input_validation import ImpossibleInputError, InputRejection, reject, require_format

logger = logging.getLogger(__name__)

_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

#: What a number is once its spacing and punctuation are gone: E.164 allows at most 15 digits, and
#: nothing shorter than four can be dialled. Deliberately looser than E.164 so a national format
#: Twilio itself would accept is still sent.
_DIALABLE = re.compile(r"\+?\d{4,15}")
_NUMBER_PUNCTUATION = re.compile(r"[\s().-]")


@dataclass(slots=True, kw_only=True)
class TwilioGateway(Gateway):
    """Base gateway for Twilio's Messaging API (shared by SMS and WhatsApp)."""

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
            True if Twilio accepted the message for delivery, False on failure (logged, never raised - a failed notification shouldn't break the caller's own request/task) or when the number cannot be dialled or the text is empty (counted, never sent).
        """
        # Narrowed to local variables (rather than trusting __post_init__'s check
        # of the instance attributes) so the type checker can see these are
        # non-None at the point of use.
        account_sid, auth_token, from_number = self.account_sid, self.auth_token, self.from_number
        if not account_sid or not auth_token or not from_number:
            raise ValueError(f"{type(self).__name__} is not configured.")

        service = type(self).service_key or "twilio"
        try:
            # A number that cannot be dialled, or no text, is a 400 from Twilio; refused here, it is counted without being sent.
            require_format(service, "To", _NUMBER_PUNCTUATION.sub("", to_number.removeprefix("whatsapp:")), _DIALABLE)
            if not body:
                reject(service, InputRejection.EMPTY_QUERY, "Body is empty")
        except ImpossibleInputError:
            return False

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
