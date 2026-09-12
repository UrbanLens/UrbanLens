"""Reading and acknowledging a user's notifications, and editing delivery preferences.
Extracted from ``controllers.notifications`` so the bell dropdown and the external API share one implementation instead of the API re-deriving queries that already existed as view bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import Q

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Status
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.services.core.keyset_cursor import InvalidCursorError, decode_cursor, encode_cursor

if TYPE_CHECKING:
    from uuid import UUID

    from urbanlens.dashboard.models.profile.model import Profile

#: Default page size for :func:`list_notifications`.
DEFAULT_NOTIFICATION_PAGE_SIZE = 50

#: Hard ceiling on a caller-supplied page size.
MAX_NOTIFICATION_PAGE_SIZE = 100


class InvalidNotificationCursorError(InvalidCursorError):
    """A :func:`list_notifications` pagination cursor didn't decode, or wasn't ours.
    Kept distinct from the base :class:`InvalidCursorError` so a catch site here can dispatch on this feed specifically."""


@dataclass(frozen=True, slots=True)
class NotificationPage:
    """One page of a profile's notifications plus its continuation token."""

    notifications: list[NotificationLog]
    next_cursor: str | None


def list_notifications(
    profile: Profile,
    *,
    unread_only: bool = False,
    cursor: str | None = None,
    limit: int = DEFAULT_NOTIFICATION_PAGE_SIZE,
) -> NotificationPage:
    """Return one page of ``profile``'s notifications, newest first.
    Ordered by ``(-created, -pk)`` so the keyset stays deterministic when several notifications share a timestamp (a batch fan-out routinely produces those).

    Args:
        profile: The owner whose notifications to read.
        unread_only: Restrict to unread rows.
        cursor: Opaque continuation token from a previous page.
        limit: Page size, clamped to :data:`MAX_NOTIFICATION_PAGE_SIZE`.

    Returns:
        The page of notifications and the cursor for the next one (``None`` when the page is the last).

    Raises:
        InvalidNotificationCursorError: ``cursor`` is malformed or was never ours."""
    limit = min(max(int(limit or DEFAULT_NOTIFICATION_PAGE_SIZE), 1), MAX_NOTIFICATION_PAGE_SIZE)

    query = NotificationLog.objects.for_profile(profile).select_related("source_profile")
    if unread_only:
        query = query.unread()
    if cursor:
        try:
            stamp, pk = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise InvalidNotificationCursorError(f"cursor {cursor!r} for profile {profile.pk} failed to decode: {exc}") from exc
        # Descending keyset: strictly "older than" the last row of the prior page.
        query = query.filter(Q(created__lt=stamp) | Q(created=stamp, pk__lt=pk))

    rows = list(query.order_by("-created", "-pk")[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    next_cursor = encode_cursor(rows[-1].created, rows[-1].pk) if has_more and rows else None
    return NotificationPage(notifications=rows, next_cursor=next_cursor)


def mark_notification_read(profile: Profile, notification_uuid: UUID | str) -> bool:
    """Mark one of ``profile``'s own notifications read.
    The profile filter is applied *before* the uuid match, so a uuid belonging to another profile behaves exactly like one that does not exist - the caller learns nothing about notifications that are not theirs.

    Args:
        profile: The owner acting on the notification.
        notification_uuid: The notification's public uuid.

    Returns:
        True when a row was updated; False when nothing matched (including an already-read row belonging to the caller, which needs no write)."""
    return NotificationLog.objects.for_profile(profile).filter(uuid=notification_uuid).exclude(status=Status.READ).update(status=Status.READ) > 0


def mark_all_read(profile: Profile) -> int:
    """Mark every one of ``profile``'s unread notifications read.

    Args:
        profile: The owner whose notifications to clear.

    Returns:
        The number of rows updated.
    """
    return NotificationLog.objects.for_profile(profile).unread().mark_read()


def unread_count(profile: Profile) -> int:
    """Count ``profile``'s unread notifications.

    Args:
        profile: The owner whose notifications to count.

    Returns:
        The number of unread rows.
    """
    return NotificationLog.objects.for_profile(profile).unread().count()


def dismiss_notification(notification_id: int | None) -> bool:
    """Mark a notification dismissed so it leaves the bell inbox.

    Args:
        notification_id: Primary key of the ``NotificationLog`` to dismiss.

    Returns:
        True when a row was updated."""
    if not notification_id:
        return False
    return NotificationLog.objects.filter(pk=notification_id).exclude(status=Status.DISMISSED).mark_dismissed() > 0


def inbox_notifications(profile: Profile, *, limit: int = 20) -> list[NotificationLog]:
    """Newest non-dismissed notifications for the bell dropdown.

    Args:
        profile: The owner whose inbox to read.
        limit: Maximum rows to return.

    Returns:
        Up to ``limit`` notifications, newest first, with display relations loaded.
    """
    return list(NotificationLog.objects.for_profile(profile).for_inbox().for_display().order_by("-created")[:limit])


def preference_field_names() -> tuple[str, ...]:
    """The notification-preference stems that actually exist on the model.
    Derived by introspecting ``NotificationPreference`` for char fields whose choices are ``DeliveryPreference``, rather than repeating a list.

    Returns:
        The stems in model field-declaration order."""
    delivery_choices = list(DeliveryPreference.choices)

    fields = NotificationPreference._meta.get_fields()  # noqa: SLF001
    return tuple(field.name for field in fields if list(getattr(field, "choices", None) or ()) == delivery_choices)


def get_preferences(profile: Profile) -> NotificationPreference:
    """Return ``profile``'s preference row, creating the default one if absent.

    Args:
        profile: The owner whose preferences to read.

    Returns:
        The profile's ``NotificationPreference``.
    """
    prefs, _ = NotificationPreference.objects.get_or_create(profile=profile)
    return prefs


def update_preferences(profile: Profile, changes: dict[str, Any]) -> NotificationPreference:
    """Apply a partial preference update and return the saved row.
    WhatsApp and SMS cannot be enabled without a number to deliver to, so those flags are forced off when the profile has no corresponding number - server-side, regardless of what the caller submitted.

    Args:
        profile: The owner whose preferences to change.
        changes: Mapping of ``{stem: {"delivery": ..., "whatsapp": ..., "sms": ...}}``.

    Returns:
        The updated ``NotificationPreference``."""
    prefs = get_preferences(profile)
    can_whatsapp = bool(profile.whatsapp_number)
    can_sms = bool(profile.phone_number)

    touched: list[str] = []
    for stem in preference_field_names():
        change = changes.get(stem)
        if not change:
            continue
        if "delivery" in change:
            setattr(prefs, stem, change["delivery"])
            touched.append(stem)
        if "whatsapp" in change:
            setattr(prefs, f"{stem}_whatsapp", can_whatsapp and bool(change["whatsapp"]))
            touched.append(f"{stem}_whatsapp")
        if "sms" in change:
            setattr(prefs, f"{stem}_sms", can_sms and bool(change["sms"]))
            touched.append(f"{stem}_sms")

    if touched:
        prefs.save(update_fields=[*touched, "updated"])
    return prefs


def serialize_preferences(prefs: NotificationPreference) -> dict[str, dict[str, Any]]:
    """Shape a preference row as the nested per-stem document the API serves.

    Args:
        prefs: The row to serialize.

    Returns:
        ``{stem: {"delivery": str, "whatsapp": bool, "sms": bool}}`` for every stem :func:`preference_field_names` reports."""
    return {
        stem: {
            "delivery": getattr(prefs, stem),
            "whatsapp": getattr(prefs, f"{stem}_whatsapp"),
            "sms": getattr(prefs, f"{stem}_sms"),
        }
        for stem in preference_field_names()
    }
