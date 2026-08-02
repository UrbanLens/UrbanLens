"""Shared WhatsApp/SMS dispatch helpers for per-notification-type delivery preferences.

Mirrors the inline email-sending pattern already used by each notification
call site (e.g. ``services/safety.py``'s ``_send_email``): a thin wrapper
around a ``Gateway`` that no-ops quietly rather than raising, since a missing
destination number or unconfigured Twilio credentials shouldn't break the
caller's own request/task any more than a missing email address does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def send_whatsapp(profile: Profile, body: str) -> None:
    """Send a WhatsApp notification to a profile's configured number, if any.

    Args:
        profile: Recipient whose ``whatsapp_number`` (if set) is the destination.
        body: Message text.
    """
    if not profile.whatsapp_number:
        return
    from urbanlens.dashboard.services.apis.messaging.whatsapp import WhatsAppGateway

    try:
        WhatsAppGateway().send(profile.whatsapp_number, body)
    except ValueError:
        logger.debug("WhatsApp notification skipped for profile %s: Twilio not configured", profile.pk)


def send_sms(profile: Profile, body: str) -> None:
    """Send an SMS notification to a profile's configured phone number, if any.

    Args:
        profile: Recipient whose ``phone_number`` (if set) is the destination.
        body: Message text.
    """
    if not profile.phone_number:
        return
    from urbanlens.dashboard.services.apis.messaging.sms import SmsGateway

    try:
        SmsGateway().send(profile.phone_number, body)
    except ValueError:
        logger.debug("SMS notification skipped for profile %s: Twilio not configured", profile.pk)
