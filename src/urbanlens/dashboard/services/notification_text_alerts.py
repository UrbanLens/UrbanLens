"""Delayed WhatsApp/SMS alerts for site notifications, driven by the per-type toggles.

``NotificationPreference`` has carried independent ``<type>_whatsapp`` /
``<type>_sms`` opt-in booleans for every notification type since the settings
UI shipped, but only the safety check-in and direct-message paths ever read
them - every other toggle was stored and silently ignored (docs/PROBLEMS.md;
decision 2026-07-23: wire them all).

This module is the generic counterpart of the DM implementation in
``services.direct_messages`` and follows the same shape:

* Scheduling hooks in centrally (a ``post_save`` signal on ``NotificationLog``
  - see ``models/notifications/signals.py``) rather than at each of the many
  notification-creating call sites, so every current and future notification
  type with a toggle pair is covered automatically.
* Delivery is delayed (:data:`ALERT_DELAY_SECONDS`) and re-checked: a user who
  reads the notification on-site in the meantime never gets a text.
* Sends are debounced per (recipient, type) so a burst (ten pins shared at
  once) costs one billed text, not ten.
* The text body is the notification's ``title`` only - titles carry
  recipient-masked identity where relevant (baked in at creation time), and
  the body may contain more detail than belongs on a third-party carrier.

``NotificationType.MESSAGE`` is deliberately excluded: DM alerts keep their
own pipeline (per-sender streak debounce, mute checks, sender masking) in
``services.direct_messages``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import cache

if TYPE_CHECKING:
    from urbanlens.dashboard.models.notifications.model import NotificationLog

logger = logging.getLogger(__name__)

#: NotificationType values that have a ``<type>_whatsapp``/``<type>_sms``
#: toggle pair on NotificationPreference (the enum values double as the
#: preference field prefixes). MESSAGE is handled by the DM pipeline instead.
TEXT_ALERTABLE_TYPES: frozenset[str] = frozenset(
    {
        "trip_updated",
        "friend_request",
        "comment_reply",
        "comment_liked",
        "friend_accepted",
        "added_to_trip",
        "wiki_updated",
        "pin_shared",
        "visit_suggested",
        "wiki_safety_checkin",
    },
)

#: How long after an unread notification lands before the text fires, giving a
#: logged-in user a chance to read it organically first. Matches the DM flow's
#: EMAIL_DELAY_SECONDS.
ALERT_DELAY_SECONDS = 120

#: Debounce window per (recipient, type): a burst of same-type notifications
#: (a busy trip thread, a multi-pin share) costs one billed text. Unlike the
#: DM streak marker (cleared when the conversation is viewed), this is a plain
#: TTL - there's no single "the user looked" event shared by every type.
DEBOUNCE_TTL_SECONDS = 60 * 60 * 6


def _debounce_key(profile_id: int, notification_type: str) -> str:
    """Cache key marking "already texted this recipient about this type recently"."""
    return f"notif_text_alert:{profile_id}:{notification_type}"


def is_text_alert_debounced(profile_id: int, notification_type: str) -> bool:
    """Whether a recent same-type text already went to this recipient.

    Args:
        profile_id: The recipient profile's pk.
        notification_type: The NotificationType value.

    Returns:
        True when a text for this (recipient, type) fired within the window.
    """
    return bool(cache.get(_debounce_key(profile_id, notification_type)))


def _enabled_channels(notification: NotificationLog) -> tuple[bool, bool]:
    """The recipient's (whatsapp, sms) toggle states for this notification's type.

    Args:
        notification: The notification whose recipient's preferences to read.

    Returns:
        Tuple of booleans; (False, False) when the type has no toggle pair or
        the recipient has no preference row.
    """
    if notification.notification_type not in TEXT_ALERTABLE_TYPES or notification.profile is None:
        return False, False
    try:
        prefs = notification.profile.notification_preferences
    except AttributeError:
        return False, False
    prefix = notification.notification_type
    return bool(getattr(prefs, f"{prefix}_whatsapp", False)), bool(getattr(prefs, f"{prefix}_sms", False))


def schedule_notification_text_alerts(notification: NotificationLog) -> None:
    """Queue the delayed WhatsApp/SMS alert for a freshly created notification.

    Cheap no-op for the overwhelmingly common cases (type has no toggles, or
    the recipient left both off - the default); otherwise enqueues the
    re-checking Celery task with a countdown. Broker failures are swallowed by
    ``safely_enqueue_task`` - a text alert must never break the caller that
    created the notification.

    Args:
        notification: The just-inserted, unread NotificationLog row.
    """
    wants_whatsapp, wants_sms = _enabled_channels(notification)
    if not (wants_whatsapp or wants_sms):
        return

    from urbanlens.dashboard.services.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import send_notification_text_alerts_if_unread

    safely_enqueue_task(send_notification_text_alerts_if_unread, notification.pk, countdown=ALERT_DELAY_SECONDS)


def send_notification_text_alerts_now(notification: NotificationLog) -> None:
    """Send the WhatsApp/SMS alert(s) for a notification and set the debounce marker.

    Called by the Celery task once the delay has elapsed - the notification
    must still be unread and not debounced (both checked by the caller). The
    body is the notification title only; details stay on-site rather than
    traveling through a third-party carrier.

    Args:
        notification: The still-unread notification to alert about.
    """
    from urbanlens.dashboard.services.notification_delivery import send_sms, send_whatsapp

    profile = notification.profile
    wants_whatsapp, wants_sms = _enabled_channels(notification)
    if profile is None or not (wants_whatsapp or wants_sms):
        return

    cache.set(_debounce_key(profile.pk, notification.notification_type), 1, timeout=DEBOUNCE_TTL_SECONDS)

    body = f"UrbanLens: {notification.title}. Open the site for details."
    if wants_whatsapp:
        send_whatsapp(profile, body)
    if wants_sms:
        send_sms(profile, body)
