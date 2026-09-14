"""Shared email/WhatsApp/SMS dispatch helpers for per-notification-type delivery preferences."""

from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def send_notification_email(recipient: Profile, *, title: str, body_text: str, url: str | None = None, action_label: str = "View on UrbanLens") -> None:
    """Email *recipient* a generic notification.

    Args:
        recipient: Who to email - uses ``recipient.user.email``.
        title: Subject line and email heading - the same string the in-app notification's own ``title`` already is.
        body_text: The notification's own ``message`` text.
        url: Site-relative path (e.g. from ``reverse()``) the action button should link to, or None for types with no single deep link (e.g. a visit suggestion) - falls back to the site root.
        action_label: Button text."""
    recipient_email = recipient.user.email if recipient.user else None
    if not recipient_email:
        return
    action_url = f"{settings.SITE_URL.rstrip('/')}{url}" if url else settings.SITE_URL.rstrip("/")
    text_body = f"{body_text}\n\n{action_url}" if body_text else action_url
    try:
        html_body = render_to_string(
            "dashboard/email/notification.html",
            {"recipient": recipient, "title": title, "body_text": body_text, "action_url": action_url, "action_label": action_label},
        )
        msg = EmailMultiAlternatives(subject=title, body=text_body, from_email=None, to=[recipient_email])
        msg.attach_alternative(html_body, "text/html")
        msg.send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send notification email to %s", recipient_email)
    except Exception:
        # A caller-supplied title/body isn't validated against the template ahead of time, so a
        # rendering bug must be logged like every other delivery failure here, not raised uncaught
        # into the notify function that's often mid-write on the actual event (a friend request, an
        # award) this email is secondary to.
        logger.exception("Failed to render/send notification email to %s", recipient_email)


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
