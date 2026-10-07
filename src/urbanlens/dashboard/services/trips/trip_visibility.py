"""Per-viewer visibility of a trip activity's location.
Shared by the trip controllers (activities panel, map data) and anything else - like AI trip suggestions - that must never show a viewer a location their trip-mate chose not to reveal to them specifically."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.profile.model import VisibilityChoice

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import TripActivity


#: What a viewer who may not see an activity's location is shown instead.
HIDDEN_ACTIVITY_TITLE = "Secret Location"


def shown_activity_title(activity: TripActivity, *, hidden: bool) -> str | None:
    """The activity's own stored title as this viewer may see it.

    A title filled in from the place (``TripActivity.title_from_place``) names the place, so it goes wherever the
    location goes. A title the author typed was written for the other members and is kept.

    Args:
        activity: The activity being rendered.
        hidden: Whether this viewer may not see its location.

    Returns:
        The title, or None when it has none or it names a place this viewer may not see.
    """
    title = (activity.title or "").strip()
    if not title or (hidden and activity.title_from_place):
        return None
    return title


def masked_activity_title(activity: TripActivity, *, hidden: bool) -> str:
    """The activity's display title as this viewer may see it.
    An activity's ``effective_title`` falls back to its location's name, so for a hidden activity the title *is* the location - which is why masking it is not cosmetic.

    Args:
        activity: The activity being rendered.
        hidden: Whether this viewer may not see its location.

    Returns:
        A display title safe to put anywhere in the page, including in attributes the eye does not reach."""
    if not hidden:
        return activity.effective_title
    return shown_activity_title(activity, hidden=True) or HIDDEN_ACTIVITY_TITLE


def apply_trip_visibility_filter(
    sensitive: list[TripActivity],
    viewer: Profile,
    hidden_out: set[int],
) -> None:
    """Populate *hidden_out* with the IDs of activities whose location the viewer may not see, based on each adder's trip_pin_location_visibility setting.

    Args:
        sensitive: Activities already filtered to non-ANYONE visibility and non-owner viewer.
        viewer: The profile viewing the trip.
        hidden_out: Mutable set to add hidden activity IDs into."""
    from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
    from urbanlens.dashboard.models.pin.model import Pin

    # Activities where the adder's account was deleted: treat as most restrictive.
    hidden_out.update(a.id for a in sensitive if a.added_by is None)
    no_one_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.NO_ONE]
    common_pin_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.COMMON_PIN]
    friends_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.FRIENDS]
    c_friend_acts = [a for a in sensitive if a.added_by is not None and a.added_by.trip_pin_location_visibility == VisibilityChoice.COMMON_FRIEND]
    # COMMON_TRIP and ANYTHING_IN_COMMON: the viewer shares this very trip with
    # the adder, which satisfies both - treat as visible.

    hidden_out.update(act.id for act in no_one_acts)

    # Friends of the viewer always qualify for every option except NO_ONE, so
    # compute the viewer's accepted-friend ids once for all branches below.
    viewer_friend_ids: set[int] = set()
    if common_pin_acts or friends_acts or c_friend_acts:
        friend_pairs = Friendship.objects.filter(
            Q(from_profile=viewer) | Q(to_profile=viewer),
            status=FriendshipStatus.ACCEPTED,
        ).values_list("from_profile_id", "to_profile_id")
        for pair in friend_pairs:
            viewer_friend_ids.update(pair)
        viewer_friend_ids.discard(viewer.id)

    if common_pin_acts:
        # Place-aware, not a raw Location match: the viewer's own pin fifty metres away on the same
        # parcel must still qualify as "common pin" (see services.pins.common_pins.pins_sharing_a_place_with).
        from urbanlens.dashboard.models.location.model import Location

        loc_ids = {a.location_id for a in common_pin_acts if a.location_id is not None}
        loc_to_place: dict[int, int | None] = dict(Location.objects.filter(pk__in=loc_ids).values_list("pk", "place_id"))
        viewer_place_ids: set[int] = set()
        viewer_location_ids: set[int] = set()
        at_these_stops = Q(location__place_id__in={place for place in loc_to_place.values() if place is not None}) | Q(location_id__in={loc for loc, place in loc_to_place.items() if place is None})
        for location_id, place_id in Pin.objects.filter(at_these_stops, profile=viewer).values_list("location_id", "location__place_id").distinct():
            (viewer_place_ids if place_id is not None else viewer_location_ids).add(place_id if place_id is not None else location_id)
        for act in common_pin_acts:
            if act.added_by_id in viewer_friend_ids:
                continue
            act_place_id = loc_to_place.get(act.location_id) if act.location_id is not None else None
            matches = (act_place_id in viewer_place_ids) if act_place_id is not None else (act.location_id in viewer_location_ids)
            if not matches:
                hidden_out.add(act.id)

    for act in friends_acts:
        if act.added_by_id not in viewer_friend_ids:
            hidden_out.add(act.id)

    # The adders' friends, for every adder at once: one query however many trips the activities span.
    c_friend_adders = {act.added_by_id for act in c_friend_acts if act.added_by_id is not None and act.added_by_id not in viewer_friend_ids}
    adders_friends: dict[int, set[int]] = {adder: set() for adder in c_friend_adders}
    if c_friend_adders:
        for from_id, to_id in Friendship.objects.filter(
            Q(from_profile_id__in=c_friend_adders) | Q(to_profile_id__in=c_friend_adders),
            status=FriendshipStatus.ACCEPTED,
        ).values_list("from_profile_id", "to_profile_id"):
            if from_id in adders_friends:
                adders_friends[from_id].add(to_id)
            if to_id in adders_friends:
                adders_friends[to_id].add(from_id)
    for act in c_friend_acts:
        adder = act.added_by_id
        if adder is not None and adder not in viewer_friend_ids and not (viewer_friend_ids & adders_friends[adder]):
            hidden_out.add(act.id)


def viewer_hidden_activity_ids(activities: list[TripActivity], viewer: Profile) -> set[int]:
    """Convenience wrapper: compute the full hidden-activity-id set for a viewer.

    Args:
        activities: Candidate activities (any status/location state).
        viewer: The profile viewing the trip.

    Returns:
        IDs of activities whose location this viewer may not see."""
    hidden = {act.id for act in activities if act.location_hidden}
    # An activity whose adder is NULL reaches the filter too.
    # Every production path sets added_by to a real profile, so NULL means that account was deleted
    # (the FK is SET_NULL) - their setting is gone and the filter treats it as most restrictive.
    # A title taken from a place is that place even with no Location behind it, as an imported calendar event's
    # location is (P338), so it is withheld as a location would be.
    sensitive = [act for act in activities if not act.location_hidden and (act.location_id or act.title_from_place) and act.added_by_id != viewer.id and (act.added_by is None or act.added_by.trip_pin_location_visibility != VisibilityChoice.ANYONE)]
    if sensitive:
        apply_trip_visibility_filter(sensitive, viewer, hidden)
    return hidden
