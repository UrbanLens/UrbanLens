"""Critical-issue notification dispatch (admin email + Gotify push).

Lets the site admin be alerted about critical issues (e.g. a pin import failing to
process an uploaded file) without exposing the details of whatever triggered the
issue - callers should only pass high-level, non-sensitive facts (what happened and
when) and point the admin at the app logs for specifics.
"""

from __future__ import annotations

import logging
import smtplib
from typing import Final

from django.core.mail import EmailMultiAlternatives
import requests

logger = logging.getLogger(__name__)

_GOTIFY_TIMEOUT = 10
_GOTIFY_PRIORITY = 5


class NotificationEvent:
    """Registered notification event keys (see ``_EVENT_CHANNEL_FIELDS``)."""

    PIN_IMPORT_ERROR: Final = "pin_import_error"


# Maps each event key to the SiteSettings BooleanField that controls whether it is
# routed to a given channel. Add an entry here (and the matching SiteSettings
# fields) for each new notification type.
_EVENT_CHANNEL_FIELDS: dict[str, dict[str, str]] = {
    NotificationEvent.PIN_IMPORT_ERROR: {
        "email": "notify_pin_import_errors_email",
        "gotify": "notify_pin_import_errors_gotify",
    },
}


def notify(event: str, subject: str, message: str) -> None:
    """Send a critical-issue notification to every channel enabled for ``event``.

    Args:
        event: One of the ``NotificationEvent`` keys.
        subject: Short human-readable summary; also used as the Gotify title.
        message: Notification body. Must not include user-supplied content (e.g.
            uploaded file contents or names) - only non-sensitive facts such as a
            file format and timestamp. Admins can consult the app logs to
            investigate further.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    fields = _EVENT_CHANNEL_FIELDS.get(event)
    if not fields:
        logger.warning("Unknown notification event '%s'; skipping", event)
        return

    site = SiteSettings.get_current()

    if getattr(site, fields["email"], False):
        _send_email(site, subject, message)

    if getattr(site, fields["gotify"], False):
        _send_gotify(site, subject, message)


def _send_email(site, subject: str, message: str) -> None:
    """Email ``site.notify_admin_email`` with the notification, if configured."""
    if not site.notify_admin_email:
        logger.warning("Admin notification email requested but no address is configured")
        return

    try:
        EmailMultiAlternatives(
            subject=f"[UrbanLens] {subject}",
            body=message,
            from_email=None,  # Uses UL_EMAIL_FROM
            to=[site.notify_admin_email],
        ).send()
    except (smtplib.SMTPException, OSError):
        logger.exception("Failed to send admin notification email")


def _send_gotify(site, subject: str, message: str) -> None:
    """Push the notification to the configured Gotify server, if configured."""
    if not site.notify_gotify_url or not site.notify_gotify_token:
        logger.warning("Gotify notification requested but Gotify is not configured")
        return

    try:
        requests.post(
            f"{site.notify_gotify_url.rstrip('/')}/message",
            params={"token": site.notify_gotify_token},
            data={"title": subject, "message": message, "priority": _GOTIFY_PRIORITY},
            timeout=_GOTIFY_TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Failed to send Gotify notification")
