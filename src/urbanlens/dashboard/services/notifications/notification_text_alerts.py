"""Delayed WhatsApp/SMS alerts for site notifications, driven by the per-type toggles. * Scheduling hooks in centrally (a ``post_save`` signal on ``NotificationLog`` - see ``models/notifications/signals.py``) rather than at each of the many..."""

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
        "achievement_earned",
        # Was missing while its toggle pair existed and was settable, so a user could switch on
        # WhatsApp/SMS for partner invites and never get one.
        # Kept as an explicit list rather than derived from the columns, because "we send texts for
        # this" is a delivery decision - but a test asserts this set is exactly the stems with a
        "safety_ci_partner_invite",
    },
)

#: How long after an unread notification lands before the text fires, giving a
#: logged-in user a chance to read it organically first. Matches the DM flow's
#: EMAIL_DELAY_SECONDS.
ALERT_DELAY_SECONDS = 120

#: Debounce window per (recipient, type): a burst of same-type notifications (a busy trip thread, a
#: multi-pin share) costs one billed text.
#: Unlike the DM streak marker (cleared when the conversation is viewed), this is a plain TTL -
#: there's no single "the user looked" event shared by every type.
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
        True when a text for this (recipient, type) already fired within the window (or just got claimed by a concurrent caller); False when this call just claimed the marker and should proceed to send."""
    return not cache.add(_debounce_key(profile_id, notification_type), value=True, timeout=DEBOUNCE_TTL_SECONDS)


def _enabled_channels(notification: NotificationLog) -> tuple[bool, bool]:
    """The recipient's (whatsapp, sms) toggle states for this notification's type.

    Args:
        notification: The notification whose recipient's preferences to read.

    Returns:
        Tuple of booleans; (False, False) when the type has no toggle pair or the recipient has no preference row."""
    if notification.notification_type not in TEXT_ALERTABLE_TYPES or notification.profile is None:
        return False, False
    try:
        prefs = notification.profile.notification_preferences
    except AttributeError:
        return False, False
    # Derived from the enum *member name*, not its value.
    # The two agree for 31 of the 32 types, but `SAFETY_CHECKIN_PARTNER_INVITE` has the value
    # `safety_ci_partner_invite` while its columns are `safety_checkin_partner_invite*` - and every
    # other consumer of these preferences reads them by the member-style name.
    from urbanlens.dashboard.models.notifications.meta.type import NotificationType

    prefix = NotificationType(notification.notification_type).name.lower()
    return bool(getattr(prefs, f"{prefix}_whatsapp", False)), bool(getattr(prefs, f"{prefix}_sms", False))


def schedule_notification_text_alerts(notification: NotificationLog) -> None:
    """Queue the delayed WhatsApp/SMS alert for a freshly created notification.
    Cheap no-op for the overwhelmingly common cases (type has no toggles, or the recipient left both off - the default); otherwise enqueues the re-checking Celery task with a countdown.

    Args:
        notification: The just-inserted, unread NotificationLog row."""
    wants_whatsapp, wants_sms = _enabled_channels(notification)
    if not (wants_whatsapp or wants_sms):
        return

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import send_notification_text_alerts_if_unread

    safely_enqueue_task(send_notification_text_alerts_if_unread, notification.pk, countdown=ALERT_DELAY_SECONDS)


def send_notification_text_alerts_now(notification: NotificationLog) -> None:
    """Send the WhatsApp/SMS alert(s) for a notification.
    The body is the notification title only; details stay on-site rather than traveling through a third-party carrier.

    Args:
        notification: The still-unread notification to alert about."""
    from urbanlens.dashboard.services.notifications.notification_delivery import send_sms, send_whatsapp

    profile = notification.profile
    wants_whatsapp, wants_sms = _enabled_channels(notification)
    if profile is None or not (wants_whatsapp or wants_sms):
        return

    body = f"UrbanLens: {notification.title}. Open the site for details."
    if wants_whatsapp:
        send_whatsapp(profile, body)
    if wants_sms:
        send_sms(profile, body)
