"""Per-viewer visibility of a trip activity's location.

An activity's location is hidden from a given viewer when its adder set
``trip_pin_location_visibility`` to something more restrictive than "anyone",
unless the viewer qualifies (friend, shares the pin, shares the trip - see
below). Shared by the trip controllers (activities panel, map data) and
anything else - like AI trip suggestions - that must never show a viewer a
location their trip-mate chose not to reveal to them specifically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.profile.model import VisibilityChoice

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import TripActivity


def apply_trip_visibility_filter(
    sensitive: list[TripActivity],
    viewer: Profile,
    hidden_out: set[int],
) -> None:
    """Populate *hidden_out* with the IDs of activities whose location the viewer
    may not see, based on each adder's trip_pin_location_visibility setting.

    Args:
        sensitive: Activities already filtered to non-ANYONE visibility and
                   non-owner viewer.
        viewer:    The profile viewing the trip.
        hidden_out: Mutable set to add hidden activity IDs into.
    """
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
        loc_ids = {a.location_id for a in common_pin_acts}
        viewer_locs = set(
            Pin.objects.filter(profile=viewer, location_id__in=loc_ids).values_list("location_id", flat=True),
        )
        for act in common_pin_acts:
            if act.added_by_id not in viewer_friend_ids and act.location_id not in viewer_locs:
                hidden_out.add(act.id)

    for act in friends_acts:
        if act.added_by_id not in viewer_friend_ids:
            hidden_out.add(act.id)

    for act in c_friend_acts:
        if act.added_by_id in viewer_friend_ids:
            continue
        # Adder's friends
        adder_friends = set(
            Friendship.objects.filter(
                Q(from_profile_id=act.added_by_id) | Q(to_profile_id=act.added_by_id),
                status=FriendshipStatus.ACCEPTED,
            ).values_list("from_profile_id", "to_profile_id"),
        )
        adder_flat: set[int] = set()
        for pair in adder_friends:
            adder_flat.update(pair)
        if act.added_by_id is not None:
            adder_flat.discard(act.added_by_id)

        if not (viewer_friend_ids & adder_flat):
            hidden_out.add(act.id)


def viewer_hidden_activity_ids(activities: list[TripActivity], viewer: Profile) -> set[int]:
    """Convenience wrapper: compute the full hidden-activity-id set for a viewer.

    Combines the activity's own ``location_hidden`` flag with the per-adder
    visibility rule above - the two checks every call site needs together.

    Args:
        activities: Candidate activities (any status/location state).
        viewer: The profile viewing the trip.

    Returns:
        IDs of activities whose location this viewer may not see.
    """
    hidden = {act.id for act in activities if act.location_hidden}
    sensitive = [
        act
        for act in activities
        if not act.location_hidden and act.added_by_id and act.added_by_id != viewer.id and act.added_by and act.added_by.trip_pin_location_visibility != VisibilityChoice.ANYONE and act.location_id
    ]
    if sensitive:
        apply_trip_visibility_filter(sensitive, viewer, hidden)
    return hidden
