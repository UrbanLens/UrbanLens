"""Notifications raised by activity on a comment thread - replies and reactions.

This module exists to break a layering inversion. The implementations below
used to live in ``controllers.comments``, and both ``services.comments.comments`` and
``services.trips.trip_comments`` reached *up* into that controller to call them with
function-local imports (function-local precisely because a module-level import
would have made the cycle fatal at import time). A service importing a
controller means the business rule is only reachable by first loading a view
layer, so the next caller that cannot do that - a Celery task, a management
command, the external API - grows its own copy instead, and the copies drift.

The rules that live here, and only here:

- **Never notify someone about their own action.** Both helpers return early
  when the actor is also the recipient. Without this a user replying to their
  own comment, or re-reacting to their own, notifies themselves.
- **Honour the recipient's delivery preference.** ``comment_reply`` and
  ``comment_liked`` on ``notification_preferences`` can be set to
  ``DeliveryPreference.NONE``, which must suppress the row entirely rather than
  writing it and hiding it at render time - a stored notification still shows
  up in counts and digests.
- **One deep-link builder for all three comment kinds.** Pin, wiki and trip
  comments live in different models and reverse to different routes;
  :func:`comment_url` is the single place that knows the mapping, so a
  notification can never link to the wrong page or, worse, to a page the
  recipient can't open.

Both notify functions accept either a ``Comment`` (pin/wiki) or a
``TripComment``, which is why they read the author off whichever of
``profile``/``author`` the instance actually has rather than assuming one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.urls import NoReverseMatch, reverse

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def comment_url(comment: Any) -> str:
    """Build the page URL (with ``#comment-<id>`` anchor) for any comment kind.

    Dispatches on which foreign key the instance actually carries rather than
    on its class, so a ``TripComment`` and a ``Comment`` can both be passed
    without the caller having to know which it holds.

    Args:
        comment: A ``Comment`` (pin or wiki) or a ``TripComment``.

    Returns:
        The absolute path to the comment's page including its anchor, or an
        empty string when no route could be built. An empty URL is deliberate:
        a notification with no link is degraded but harmless, whereas raising
        would abort the reply or reaction that triggered it - the user's actual
        action - over a broken link.
    """
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
        comment: A ``Comment`` (which names its author ``profile``) or a
            ``TripComment`` (which names it ``author``).

    Returns:
        The authoring profile, or None when the comment has no author (trip
        comments keep their row when the author is removed).
    """
    if hasattr(comment, "profile"):
        return comment.profile
    return getattr(comment, "author", None)


def _preference(recipient: Profile, field: str) -> DeliveryPreference:
    """Read one delivery preference off *recipient*, defaulting to site delivery.

    Args:
        recipient: The profile about to be notified.
        field: The attribute name on ``notification_preferences`` to read.

    Returns:
        The stored preference, or ``DeliveryPreference.SITE`` when the profile
        has no preferences row yet. Defaulting to SITE rather than NONE keeps a
        user who never opened their settings from silently losing every
        notification.
    """
    try:
        return getattr(recipient.notification_preferences, field)
    except AttributeError:
        return DeliveryPreference.SITE


def notify_reply(actor: Profile, parent_comment: Any, reply: Any = None) -> None:
    """Tell the author of *parent_comment* that *actor* replied to it.

    Args:
        actor: The profile that posted the reply.
        parent_comment: The comment that was replied to.
        reply: The new reply, used for the deep link so the notification lands
            on the reply itself. Falls back to *parent_comment* when omitted.
    """
    recipient = _recipient_of(parent_comment)
    if recipient is None or recipient == actor:
        return
    if _preference(recipient, "comment_reply") == DeliveryPreference.NONE:
        return
    NotificationLog.objects.create(
        profile=recipient,
        notification_type=NotificationType.COMMENT_REPLY,
        title=f"{actor.username} replied to your comment",
        message=f"@{actor.username} replied to your comment.",
        url=comment_url(reply or parent_comment),
    )


def notify_reaction(actor: Profile, comment: Any) -> None:
    """Tell a comment's author that *actor* reacted to it.

    Only *adding* a reaction should call this. "Someone un-reacted to your
    comment" is not an event worth a notification, and sending one would let a
    reaction be toggled repeatedly to spam the author.

    Args:
        actor: The profile that just added a reaction.
        comment: The comment they reacted to - a ``Comment`` or ``TripComment``.
    """
    recipient = _recipient_of(comment)
    if recipient is None or recipient == actor:
        return
    if _preference(recipient, "comment_liked") == DeliveryPreference.NONE:
        return
    NotificationLog.objects.create(
        profile=recipient,
        notification_type=NotificationType.COMMENT_LIKED,
        title=f"{actor.username} reacted to your comment",
        message=f"@{actor.username} reacted to your comment.",
        url=comment_url(comment),
    )
