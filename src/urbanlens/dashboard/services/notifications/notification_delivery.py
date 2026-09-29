"""Shared email/WhatsApp/SMS dispatch helpers for per-notification-type delivery preferences."""

from __future__ import annotations

import logging
import smtplib
from typing import TYPE_CHECKING, Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.services.core.site_urls import absolute_url

if TYPE_CHECKING:
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def delivery_preference(profile: Profile, field: str, *, default: DeliveryPreference = DeliveryPreference.SITE) -> DeliveryPreference:
    """Read one ``NotificationPreference`` delivery choice for *profile*.

    Args:
        profile: The profile about to be notified.
        field: The ``DeliveryPreference`` column on ``NotificationPreference`` to read, e.g. ``"friend_request"``.
        default: What a profile with no preferences row gets.

    Returns:
        The stored choice, or *default* when the profile has no preferences row or the stored value is not a known choice.

    Raises:
        AttributeError: *field* is not a column of ``NotificationPreference``."""
    try:
        preferences = profile.notification_preferences
    except ObjectDoesNotExist:
        return default
    value = getattr(preferences, field)
    try:
        return DeliveryPreference(value)
    except ValueError:
        logger.warning("Profile %s has unknown %s delivery preference %r; using %s", profile.pk, field, value, default)
        return default


def deliver_notification(
    recipient: Profile,
    preference: DeliveryPreference,
    *,
    title: str,
    message: str,
    url: str | None = None,
    email_url: str | None = None,
    **log_fields: Any,
) -> NotificationLog | None:
    """Deliver one notification through the channels *preference* picks.

    Resolve *preference* with :func:`delivery_preference` first, so a caller can return before building the text
    when it is ``NONE``. The in-app row goes through ``NotificationLog.objects.notify``, which applies the mute
    preference; the email is queued for after commit.

    Args:
        recipient: The profile being notified.
        preference: The recipient's choice for this notification type.
        title: The row's title and the email subject.
        message: The row's message and the email body.
        url: Site-relative path for both the row and the email button.
        email_url: The email button's path when it differs from *url*.
        **log_fields: Further ``NotificationLog`` fields (``notification_type``, ``source_profile``, ...).

    Returns:
        The in-app row, or None when the preference excludes it or the recipient muted its source."""
    from urbanlens.dashboard.models.notifications.model import NotificationLog

    notification = None
    if preference.includes_site:
        if url is not None:
            log_fields["url"] = url
        notification = NotificationLog.objects.notify(profile=recipient, title=title, message=message, **log_fields)
    if preference.includes_email:
        send_notification_email(recipient, title=title, body_text=message, url=email_url or url)
    return notification


def send_notification_email(recipient: Profile, *, title: str, body_text: str, url: str | None = None, action_label: str = "View on UrbanLens") -> None:
    """Queue a generic notification email to *recipient*, sent by a worker once the current transaction commits.

    Args:
        recipient: Who to email; the address is read when the worker runs.
        title: Subject line and email heading - the same string the in-app notification's own ``title`` already is.
        body_text: The notification's own ``message`` text.
        url: Site-relative path the action button links to, or None to link the site root.
        action_label: Button text."""
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import send_notification_email_task

    profile_id = recipient.pk
    transaction.on_commit(lambda: safely_enqueue_task(send_notification_email_task, profile_id, title, body_text, url, action_label))


def send_notification_email_now(recipient: Profile, *, title: str, body_text: str, url: str | None = None, action_label: str = "View on UrbanLens") -> None:
    """Email *recipient* a generic notification now; for code already running in a worker.

    Args:
        recipient: Who to email - uses ``recipient.user.email``.
        title: Subject line and email heading - the same string the in-app notification's own ``title`` already is.
        body_text: The notification's own ``message`` text.
        url: Site-relative path (e.g. from ``reverse()``) the action button should link to, or None for types with no single deep link (e.g. a visit suggestion) - falls back to the site root.
        action_label: Button text."""
    recipient_email = recipient.user.email if recipient.user else None
    if not recipient_email:
        return
    action_url = absolute_url(url or "")
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
