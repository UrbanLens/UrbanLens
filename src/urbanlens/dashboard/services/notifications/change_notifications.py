"""Notifications that something shared changed: a trip's details or activities, a place's community wiki.

A burst of changes folds into one notification per recipient. While a recipient's notification of one actor's changes
to one trip or wiki is unread and was last added to within :data:`FOLD_WINDOW`, the next change raises its count
instead of writing a new row, so the bell, the live toast, the push and the email fire once per burst.

Producers call :func:`announce_trip_change` or :func:`announce_wiki_change` where a person's change is written, and
every ``WikiEdit`` with an editor announces itself (``models.wiki_edit.signals``); the fan-out runs on a worker once
that write commits. Automated writes (enrichment, seeding, merges) carry no person and announce nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, Importance, NotificationType, Status

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable

    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit.model import WikiEdit

logger = logging.getLogger(__name__)

#: How long after a burst's last change the next one still folds into it.
FOLD_WINDOW = timedelta(minutes=30)


@dataclass(frozen=True)
class ChangeNotice:
    """What one change tells one recipient.

    Attributes:
        title: Names the changed thing and nothing about the change; text alerts carry the title alone.
        url: Where the notification leads.
        change: What changed, as a verb phrase completing "<actor> ...", e.g. ``"added an activity"``.
    """

    title: str
    url: str
    change: str

    def message(self, actor_name: str, count: int) -> str:
        """The message for a burst of ``count`` changes by an actor shown as ``actor_name``."""
        if count <= 1:
            return f"{actor_name} {self.change}."
        return f"{actor_name} made {count} changes."


def announce_change(
    *,
    notification_type: NotificationType,
    fold_key: str,
    actor: Profile,
    recipients: Iterable[Profile | int],
    notice_for: Callable[[Profile], ChangeNotice | None],
) -> int:
    """Tell each recipient of a change, folding it into their unread notification of the same burst.

    The actor never hears of their own change, nor does anyone a block joins them with, and a recipient who muted
    the actor or switched the type off hears nothing. The actor is named as each recipient may see them.

    Args:
        notification_type: The type, which also names the recipients' preference row.
        fold_key: The changed thing, e.g. ``"trip:12"``; changes fold only within one key.
        actor: Who made the change.
        recipients: Everyone who may hear of it, as profiles or pks; the actor among them is skipped.
        notice_for: The notice for one recipient, or None when they may not hear of this change.

    Returns:
        How many recipients were told, by a new notification or a raised count.
    """
    from urbanlens.dashboard.models.friendship.blocks import SharedSpaceBlocks
    from urbanlens.dashboard.models.notifications.model import NotificationLog
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.services.notifications.notification_delivery import deliver_notification, delivery_preference
    from urbanlens.dashboard.services.profile.identity_visibility import resolve_identity_for_viewers
    from urbanlens.dashboard.services.social.friendship import profiles_muting

    ids = {recipient if isinstance(recipient, int) else recipient.pk for recipient in recipients} - {None, actor.pk}
    if not ids:
        return 0
    excluded = SharedSpaceBlocks.for_viewer(actor, among=ids).hidden_profile_ids | profiles_muting(actor, ids).profile_ids
    preference_field = NotificationType(notification_type).name.lower()
    notices: dict[int, tuple[ChangeNotice, DeliveryPreference]] = {}
    audience: list[Profile] = []
    for profile in ProfileModel.objects.filter(pk__in=ids - excluded).select_related("user", "notification_preferences").order_by("pk"):
        preference = delivery_preference(profile, preference_field)
        if preference == DeliveryPreference.NONE:
            continue
        notice = notice_for(profile)
        if notice is None:
            continue
        notices[profile.pk] = (notice, preference)
        audience.append(profile)
    if not audience:
        return 0
    identities = resolve_identity_for_viewers(actor, audience)

    with transaction.atomic():
        # Two changes landing at once would otherwise both find no burst to fold into and each start one.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"change-notice:{fold_key}:{actor.pk}"])
        now = timezone.now()
        open_bursts: dict[int | None, NotificationLog] = {
            row.profile_id: row
            for row in NotificationLog.objects.select_for_update().filter(
                profile_id__in=notices,
                notification_type=notification_type,
                source_profile=actor,
                fold_key=fold_key,
                status=Status.UNREAD,
                updated__gte=now - FOLD_WINDOW,
            )
        }
        folded: list[NotificationLog] = []
        for profile in audience:
            notice, preference = notices[profile.pk]
            name = identities[profile.pk]["display_name"] or actor.username
            burst = open_bursts.get(profile.pk)
            if burst is not None:
                burst.fold_count += 1
                burst.message = notice.message(name, burst.fold_count)
                burst.updated = now
                folded.append(burst)
                continue
            deliver_notification(
                profile,
                preference,
                title=notice.title,
                message=notice.message(name, 1),
                url=notice.url,
                notification_type=notification_type,
                source_profile=actor,
                fold_key=fold_key,
                importance=Importance.LOW,
            )
        if folded:
            NotificationLog.objects.bulk_update(folded, ["fold_count", "message", "updated"])
    return len(audience)


def _enqueue(task_name: str, *args: object) -> None:
    """Queue the fan-out for after the change commits, so a rolled-back change announces nothing."""
    from urbanlens.dashboard import tasks
    from urbanlens.dashboard.services.core.celery import safely_enqueue_task

    task = getattr(tasks, task_name)
    transaction.on_commit(lambda: safely_enqueue_task(task, *args))


# Trips


def announce_trip_change(trip: Trip, actor: Profile, change: str) -> None:
    """Tell a trip's members that ``actor`` changed it, once the change commits.

    Args:
        trip: The changed trip.
        actor: The member who changed it.
        change: What changed, completing "<actor> ...".
    """
    _enqueue("announce_trip_change_task", trip.pk, actor.pk, change)


def notify_trip_change(trip: Trip, actor: Profile, change: str) -> int:
    """Tell a trip's joined members of a change; the worker side of :func:`announce_trip_change`.

    Args:
        trip: The changed trip.
        actor: The member who changed it.
        change: What changed.

    Returns:
        How many members were told.
    """
    from urbanlens.dashboard.models.trips.model import TripMembership

    notice = ChangeNotice(title=f'"{trip.name}" was updated', url=reverse("trips.detail", kwargs={"trip_slug": trip.slug}), change=change)
    members = trip.memberships.filter(status=TripMembership.STATUS_JOINED).values_list("profile_id", flat=True)
    return announce_change(
        notification_type=NotificationType.TRIP_UPDATED,
        fold_key=f"trip:{trip.pk}",
        actor=actor,
        recipients=list(members),
        notice_for=lambda _member: notice,
    )


# Wikis


def announce_wiki_change(wiki: Wiki, actor: Profile, change: str, *, fields: Collection[str] = ()) -> None:
    """Tell the people who pinned a wiki's place that ``actor`` changed it, once the change commits.

    Args:
        wiki: The changed wiki.
        actor: Who changed it.
        change: What changed, completing "<actor> ...".
        fields: For an edit of the wiki's own fields, which ones; a viewer under concealment hears of the edit only
            when it touched a field concealment would show them.
    """
    _enqueue("announce_wiki_change_task", wiki.pk, actor.pk, change, sorted(fields))


#: What a recorded wiki edit did, by its ``WikiEdit.changes`` key, completing "<actor> ...".
_EDIT_PHRASES: dict[str, str] = {
    "alias_added": "added another name for the place",
    "alias_removed": "removed a name for the place",
    "link_added": "added a link",
    "link_removed": "removed a link",
    "markup_added": "drew on the wiki's map",
    "markup_removed": "removed a drawing from the wiki's map",
    "child_wiki_added": "added a marker",
    "child_wikis_imported": "added markers",
    "child_wiki_removed": "removed a marker",
    "child_wiki_moved": "moved a marker",
    "floorplan": "published a floor plan",
}


def describe_wiki_edit(changes: Collection[str], *, is_revert: bool = False) -> str:
    """What a recorded wiki edit did, completing "<actor> ...".

    Args:
        changes: The edit's ``WikiEdit.changes`` keys.
        is_revert: Whether the edit undid an earlier one.

    Returns:
        One phrase; a mixed edit reads as a plain update.
    """
    from urbanlens.dashboard.services.geo.wiki_boundary_edits import is_boundary_change_key
    from urbanlens.dashboard.services.wiki.wiki_edits import WIKI_EDITABLE_FIELDS

    if is_revert:
        return "reverted an edit to the wiki"
    phrases = {_EDIT_PHRASES.get(key) or ("redrew an outline" if is_boundary_change_key(key) else "edited the wiki's details" if key in WIKI_EDITABLE_FIELDS else "updated the wiki") for key in changes}
    return phrases.pop() if len(phrases) == 1 else "updated the wiki"


def announce_wiki_edit(edit: WikiEdit) -> None:
    """Announce a person's recorded wiki edit, once it commits; an automated one (no editor) announces nothing.

    Args:
        edit: The new edit.
    """
    if edit.editor_id is None:
        return
    keys = sorted(edit.changes or {})
    _enqueue("announce_wiki_change_task", edit.wiki_id, edit.editor_id, describe_wiki_edit(keys, is_revert=edit.is_revert), keys)


def wiki_audience(wiki: Wiki) -> list[int]:
    """The profiles that pinned a wiki's place, as the wiki's community count reckons them.

    Root pins only: child pins are mostly nested automatically under a site someone pinned, and a campus's thirty
    building wikis are not thirty places that person chose to follow.

    Args:
        wiki: The wiki.

    Returns:
        Profile pks.
    """
    from urbanlens.dashboard.models.pin.model import Pin

    pins = Pin.objects.root_pins()
    pins = pins.filter(location__place_id=wiki.place_id) if wiki.place_id is not None else pins.filter(location_id=wiki.location_id)
    return list(pins.values_list("profile_id", flat=True).distinct())


def notify_wiki_change(wiki: Wiki, actor: Profile, change: str, fields: Collection[str] = ()) -> int:
    """Tell a wiki's audience of a change; the worker side of :func:`announce_wiki_change`.

    A recipient under concealment hears only of a change by someone whose contributions concealment shows them, and
    of a field edit only when it touched a field concealment does not always blank. The wiki is named as that
    recipient sees it.

    Args:
        wiki: The changed wiki.
        actor: Who changed it.
        change: What changed.
        fields: The wiki fields the change wrote, if it was a field edit.

    Returns:
        How many people were told.
    """
    from urbanlens.dashboard.services.wiki.concealment import ALWAYS_UNSET, concealed_field_values, concealment_active, visible_actor_ids

    if wiki.location_id is None:
        return 0
    url = reverse("location.wiki", kwargs={"location_slug": wiki.location.slug or str(wiki.location.uuid)})
    shows_a_field = not fields or any(name not in ALWAYS_UNSET for name in fields)

    def notice_for(recipient: Profile) -> ChangeNotice | None:
        name = wiki.name
        if concealment_active(wiki, recipient):
            if actor.pk not in visible_actor_ids(recipient) or not shows_a_field:
                return None
            name = concealed_field_values(wiki, recipient).get("name") or name
        return ChangeNotice(title=f'The wiki for "{name}" was updated', url=url, change=change)

    return announce_change(
        notification_type=NotificationType.WIKI_UPDATED,
        fold_key=f"wiki:{wiki.pk}",
        actor=actor,
        recipients=wiki_audience(wiki),
        notice_for=notice_for,
    )
