"""Calendar writes owed by changes that do not save a trip or its activities.

What a member may see of a trip-mate's stop depends on the stop's own ``location_hidden`` (a save of the activity,
which ``models.trips.signals`` already pushes), on the trip-mate's ``trip_pin_location_visibility``, on friendships
(FRIENDS, COMMON_FRIEND, and a friend passing COMMON_PIN), on the member's own pins (COMMON_PIN), and on the
trip-mate's account existing at all (a stop whose adder is gone is hidden from everyone). Each of those changes
queues the push an edit would, for the trips it can affect: an auto-synced calendar then loses what it may no
longer hold. A push that finds nothing changed writes nothing to Google.

A link deleted with the trip or activity it mirrors takes the only record of its event with it, so an event
UrbanLens made is queued for deletion first (``CalendarEventDeletion``). An event an import linked from the user's
own calendar is theirs, and stays. See UrbanLens#301 ("hidden location stays on an unreached calendar").
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from urbanlens.dashboard.models.calendar_sync.model import TripCalendarLink
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity
from urbanlens.dashboard.models.trips.signals import queue_calendar_pushes_where


def _origin_model(kwargs: dict[str, Any]) -> type | None:
    """The model a delete was called on, from a delete signal's ``origin`` (an instance or a queryset)."""
    origin = kwargs.get("origin")
    if origin is None:
        return None
    model = getattr(origin, "model", type(origin))
    return model if isinstance(model, type) else None


def _account_going(kwargs: dict[str, Any]) -> bool:
    """Whether this delete is part of deleting an account, whose own calendar links go with it."""
    from django.contrib.auth.models import User

    return _origin_model(kwargs) in (User, Profile)


def _stops_added_by(profile_ids: list[int]) -> Q:
    """Links of trips holding a stop one of *profile_ids* added that names a place: a located one, or one whose title
    is a place's (an imported event's location). In one ``filter``, so both hold of the same stop."""
    return Q(trip__activities__added_by_id__in=profile_ids) & (Q(trip__activities__location__isnull=False) | Q(trip__activities__title_from_place=True))


def queue_pushes_for_stops_added_by(profile_ids: list[int]) -> None:
    """Queue the push for every auto-synced trip holding a stop that names a place, added by one of *profile_ids*.

    Args:
        profile_ids: The adders whose stops' visibility changed.
    """
    queue_calendar_pushes_where(_stops_added_by(profile_ids))


@receiver(pre_save, sender=Profile, dispatch_uid="calendar_remember_trip_pin_visibility")
def remember_trip_pin_visibility(sender: type[Profile], instance: Profile, **kwargs: Any) -> None:
    """Note whether this save changes who may see the profile's trip stops; ``Profile.save`` has already forced it."""
    instance.trip_pin_visibility_changed = False
    update_fields = kwargs.get("update_fields")
    if instance.pk is None or (update_fields is not None and "trip_pin_location_visibility" not in update_fields):
        return
    previous = Profile.objects.filter(pk=instance.pk).values_list("trip_pin_location_visibility", flat=True).first()
    instance.trip_pin_visibility_changed = previous is not None and previous != instance.trip_pin_location_visibility


@receiver(post_save, sender=Profile, dispatch_uid="calendar_push_on_trip_pin_visibility_change")
def push_on_trip_pin_visibility_change(sender: type[Profile], instance: Profile, **kwargs: Any) -> None:
    """Push every auto-synced trip with this profile's stops when who may see them changed."""
    if getattr(instance, "trip_pin_visibility_changed", False):
        queue_pushes_for_stops_added_by([instance.pk])


@receiver(pre_delete, sender=Profile, dispatch_uid="calendar_push_on_adder_account_deleted")
def push_on_adder_account_deleted(sender: type[Profile], instance: Profile, **kwargs: Any) -> None:
    """Push every auto-synced trip with this profile's stops: once the account is gone they are hidden from everyone."""
    queue_pushes_for_stops_added_by([instance.pk])


@receiver(pre_save, sender=Friendship, dispatch_uid="calendar_remember_friendship_accepted")
def remember_friendship_accepted(sender: type[Friendship], instance: Friendship, **kwargs: Any) -> None:
    """Note whether the row was an accepted friendship before this save."""
    instance.was_accepted = False
    update_fields = kwargs.get("update_fields")
    if instance.pk is None or (update_fields is not None and "status" not in update_fields):
        return
    instance.was_accepted = Friendship.objects.filter(pk=instance.pk, status=FriendshipStatus.ACCEPTED).exists()


def _push_for_friendship_ended(friendship: Friendship) -> None:
    """Push what an ended friendship can hide: either side's own calendars, and the stops either side added.

    The second covers a third member whose COMMON_FRIEND view of one side's stops relied on the other side.
    """
    pair = [friendship.from_profile_id, friendship.to_profile_id]
    queue_calendar_pushes_where(Q(profile_id__in=pair) | _stops_added_by(pair))


@receiver(post_save, sender=Friendship, dispatch_uid="calendar_push_on_friendship_ended")
def push_on_friendship_ended(sender: type[Friendship], instance: Friendship, **kwargs: Any) -> None:
    """Push when an accepted friendship stops being one: removed, or turned into a block."""
    if getattr(instance, "was_accepted", False) and instance.status != FriendshipStatus.ACCEPTED:
        _push_for_friendship_ended(instance)


@receiver(post_delete, sender=Friendship, dispatch_uid="calendar_push_on_friendship_deleted")
def push_on_friendship_deleted(sender: type[Friendship], instance: Friendship, **kwargs: Any) -> None:
    """Push when an accepted friendship row is deleted, including with one side's account."""
    if instance.status == FriendshipStatus.ACCEPTED:
        _push_for_friendship_ended(instance)


def _push_for_pin_left(pin: Pin, location_id: int | None) -> None:
    """Push the pin owner's auto-synced trips with a stop where the pin was: a COMMON_PIN stop there may now be hidden."""
    # Most accounts auto-sync no trip, so a bulk pin delete costs them one indexed lookup a pin.
    if location_id is None or not TripCalendarLink.objects.filter(profile_id=pin.profile_id, activity__isnull=True, auto_sync=True).exists():
        return
    from urbanlens.dashboard.models.location.model import Location

    place_id = Location.objects.filter(pk=location_id).values_list("place_id", flat=True).first()
    there = Q(trip__activities__location_id=location_id)
    if place_id is not None:
        there |= Q(trip__activities__location__place_id=place_id)
    queue_calendar_pushes_where(Q(profile_id=pin.profile_id) & there)


@receiver(post_save, sender=Pin, dispatch_uid="calendar_push_on_pin_moved")
def push_on_pin_moved(sender: type[Pin], instance: Pin, created: bool, **kwargs: Any) -> None:
    """Push when a pin leaves its location (``models.pin.signals.remember_child_boundary_parent`` read the old one)."""
    previous = getattr(instance, "previous_location_id", None)
    if not created and previous is not None and previous != instance.location_id:
        _push_for_pin_left(instance, previous)


@receiver(post_delete, sender=Pin, dispatch_uid="calendar_push_on_pin_deleted")
def push_on_pin_deleted(sender: type[Pin], instance: Pin, **kwargs: Any) -> None:
    """Push when a pin is deleted, unless with its owner's account, whose calendar links go too."""
    if not _account_going(kwargs):
        _push_for_pin_left(instance, instance.location_id)


@receiver(pre_delete, sender=TripCalendarLink, dispatch_uid="calendar_delete_event_with_what_it_mirrors")
def delete_event_with_what_it_mirrors(sender: type[TripCalendarLink], instance: TripCalendarLink, **kwargs: Any) -> None:
    """Queue the delete of an event UrbanLens made whose trip or activity is being deleted.

    Only then: a link deleted by itself was dealt with by its caller (``remove_trip_from_calendar``, an export
    dropping an unscheduled stop's event, a member leaving), and one deleted with its profile's account has no
    calendar left to write to.
    """
    if _origin_model(kwargs) not in (Trip, TripActivity) or not instance.google_event_id:
        return
    from urbanlens.dashboard.services.trips.calendar_sync import made_by_urbanlens, queue_calendar_event_deletion

    origin = kwargs.get("origin")
    trip_uuid = origin.uuid if isinstance(origin, Trip) and origin.pk == instance.trip_id else Trip.objects.filter(pk=instance.trip_id).values_list("uuid", flat=True).first()
    if made_by_urbanlens(instance, trip_uuid=trip_uuid):
        queue_calendar_event_deletion(instance)
