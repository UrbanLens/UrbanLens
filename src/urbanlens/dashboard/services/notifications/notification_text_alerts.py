"""Delayed WhatsApp/SMS alerts for site notifications, driven by the per-type toggles.

``NotificationPreference`` has carried independent ``<type>_whatsapp`` /
``<type>_sms`` opt-in booleans for every notification type since the settings
UI shipped, but only the safety check-in and direct-message paths ever read
them - every other toggle was stored and silently ignored (docs/PROBLEMS.md;
decision 2026-07-23: wire them all).

This module is the generic counterpart of the DM implementation in
``services.messaging.direct_messages`` and follows the same shape:

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
``services.messaging.direct_messages``.
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
        "achievement_earned",
        # Was missing while its toggle pair existed and was settable, so a user
        # could switch on WhatsApp/SMS for partner invites and never get one.
        # Kept as an explicit list rather than derived from the columns, because
        # "we send texts for this" is a delivery decision - but a test asserts
        # this set is exactly the stems with a toggle pair, minus MESSAGE, so a
        # new stem cannot be silently omitted the way this one was.
        "safety_ci_partner_invite",
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

    Checks and claims the debounce marker in one atomic ``cache.add`` - two
    Celery workers racing on the same (recipient, type) within the window
    can't both pass: only the first caller's ``cache.add`` succeeds (winning
    the right to send), and it sets the marker in that same step so every
    other racing caller sees it immediately rather than in a later, separate
    ``cache.set``.

    Args:
        profile_id: The recipient profile's pk.
        notification_type: The NotificationType value.

    Returns:
        True when a text for this (recipient, type) already fired within the
        window (or just got claimed by a concurrent caller); False when this
        call just claimed the marker and should proceed to send.
    """
    return not cache.add(_debounce_key(profile_id, notification_type), value=True, timeout=DEBOUNCE_TTL_SECONDS)


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
    # Derived from the enum *member name*, not its value. The two agree for 31 of
    # the 32 types, but `SAFETY_CHECKIN_PARTNER_INVITE` has the value
    # `safety_ci_partner_invite` while its columns are `safety_checkin_partner_invite*`
    # - and every other consumer of these preferences reads them by the member-style
    # name. Deriving from the value made this one lookup miss, and `getattr`'s False
    # default reported it as "user does not want text alerts", so a user who had
    # explicitly enabled WhatsApp/SMS for partner invites silently never got them.
    # Resolving by name fixes that type and changes nothing for the other 31.
    from urbanlens.dashboard.models.notifications.meta.type import NotificationType

    prefix = NotificationType(notification.notification_type).name.lower()
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

    from urbanlens.dashboard.services.core.celery import safely_enqueue_task
    from urbanlens.dashboard.tasks import send_notification_text_alerts_if_unread

    safely_enqueue_task(send_notification_text_alerts_if_unread, notification.pk, countdown=ALERT_DELAY_SECONDS)


def send_notification_text_alerts_now(notification: NotificationLog) -> None:
    """Send the WhatsApp/SMS alert(s) for a notification.

    Called by the Celery task once the delay has elapsed - the notification
    must still be unread and not debounced (both checked by the caller via
    ``is_text_alert_debounced``, which also claims the debounce marker
    atomically at that point - there is nothing left to mark here). The body
    is the notification title only; details stay on-site rather than
    traveling through a third-party carrier.

    Args:
        notification: The still-unread notification to alert about.
    """
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
