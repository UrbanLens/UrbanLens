"""Notifications raised by activity on a comment thread - replies and reactions."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.urls import NoReverseMatch, reverse

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.notifications.notification_delivery import send_notification_email

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def comment_url(comment: Any) -> str:
    """Build the page URL (with ``#comment-<id>`` anchor) for any comment kind.
    Dispatches on which foreign key the instance actually carries rather than on its class, so a ``TripComment`` and a ``Comment`` can both be passed without the caller having to know which it holds.

    Args:
        comment: A ``Comment`` (pin or wiki) or a ``TripComment``.

    Returns:
        The absolute path to the comment's page including its anchor, or an empty string when no route could be built."""
    anchor = f"#comment-{comment.id}"
    try:
        if getattr(comment, "trip_id", None):
            return reverse("trips.detail", kwargs={"trip_slug": comment.trip.slug}) + anchor
        if getattr(comment, "pin_id", None):
            return reverse("pin.details", kwargs={"pin_slug": comment.pin.slug or str(comment.pin.uuid)}) + anchor
        if getattr(comment, "wiki_id", None) and comment.wiki.location_id:
            return reverse("location.wiki", kwargs={"location_slug": comment.wiki.location.slug or str(comment.wiki.location.uuid)}) + anchor
    except NoReverseMatch:
        logger.warning("Could not build comment URL for comment %s", comment.id)
    return ""


def _recipient_of(comment: Any) -> Profile | None:
    """Return the profile that authored *comment*, whichever field holds it.

    Args:
        comment: A ``Comment`` (which names its author ``profile``) or a ``TripComment`` (which names it ``author``).

    Returns:
        The authoring profile, or None when the comment has no author (trip comments keep their row when the author is removed)."""
    if hasattr(comment, "profile"):
        return comment.profile
    return getattr(comment, "author", None)


def _preference(recipient: Profile, field: str) -> DeliveryPreference:
    """Read one delivery preference off *recipient*, defaulting to site delivery.

    Args:
        recipient: The profile about to be notified.
        field: The attribute name on ``notification_preferences`` to read.

    Returns:
        The stored preference, or ``DeliveryPreference.SITE`` when the profile has no preferences row yet."""
    try:
        return getattr(recipient.notification_preferences, field)
    except AttributeError:
        return DeliveryPreference.SITE


def _actor_names(recipient: Profile, actor: Profile) -> tuple[str, str]:
    """How *actor* may be named to *recipient*, as (display name, handle).
    The comment list resolves authors through ``resolve_visible_identities`` and the template renders the masked name when it says to, so naming the actor outright here would contradict the very thread the notification links to.

    Args:
        recipient: The profile being notified.
        actor: The profile that replied or reacted.

    Returns:
        ``(name, handle)`` - the handle is ``@name`` only when the recipient may actually see who *actor* is; "@Member 2" reads like a real mention and is not one."""
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_visible_identity

    identity = resolve_visible_identity(recipient, actor)
    name = identity["display_name"] or actor.username
    return name, (name if identity["is_masked"] else f"@{name}")


def notify_reply(actor: Profile, parent_comment: Any, reply: Any = None) -> None:
    """Tell the author of *parent_comment* that *actor* replied to it.

    Args:
        actor: The profile that posted the reply.
        parent_comment: The comment that was replied to.
        reply: The new reply, used for the deep link so the notification lands on the reply itself."""
    recipient = _recipient_of(parent_comment)
    if recipient is None or recipient == actor:
        return
    pref = _preference(recipient, "comment_reply")
    if pref == DeliveryPreference.NONE:
        return
    name, handle = _actor_names(recipient, actor)
    title = f"{name} replied to your comment"
    body = f"{handle} replied to your comment."
    url = comment_url(reply or parent_comment)

    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=recipient,
            notification_type=NotificationType.COMMENT_REPLY,
            title=title,
            message=body,
            url=url,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(recipient, title=title, body_text=body, url=url)


def notify_reaction(actor: Profile, comment: Any) -> None:
    """Tell a comment's author that *actor* reacted to it.

    Args:
        actor: The profile that just added a reaction.
        comment: The comment they reacted to - a ``Comment`` or ``TripComment``."""
    recipient = _recipient_of(comment)
    if recipient is None or recipient == actor:
        return
    pref = _preference(recipient, "comment_liked")
    if pref == DeliveryPreference.NONE:
        return
    name, handle = _actor_names(recipient, actor)
    title = f"{name} reacted to your comment"
    body = f"{handle} reacted to your comment."
    url = comment_url(comment)

    if pref in (DeliveryPreference.SITE, DeliveryPreference.BOTH):
        NotificationLog.objects.notify(
            profile=recipient,
            notification_type=NotificationType.COMMENT_LIKED,
            title=title,
            message=body,
            url=url,
        )
    if pref in (DeliveryPreference.EMAIL, DeliveryPreference.BOTH):
        send_notification_email(recipient, title=title, body_text=body, url=url)
