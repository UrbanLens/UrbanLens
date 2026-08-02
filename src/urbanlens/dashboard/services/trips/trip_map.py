"""The trip map's point set, shared byte-for-byte by both map surfaces.

``controllers.trip.TripMapDataView`` (the web map's ``map-data/`` fetch) and
``external_api.views.TripMapView`` (the mobile map) both return exactly what
:func:`build_trip_map_points` produces, with no per-surface reshaping. That is
deliberate: the two maps draw the same markers with the same numbering and the
same drag affordances, and a divergence would show up as the app and the site
disagreeing about where a trip's stops are.

``tests.hypothesis.test_trip_map_parity`` asserts the two payloads are equal
for the same fixture, so the shared-shape guarantee is enforced rather than
merely intended.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.trips.model import TripActivity
from urbanlens.dashboard.services.trips.trip_activities import activity_queryset
from urbanlens.dashboard.services.trips.trip_legs import activity_coords
from urbanlens.dashboard.services.trips.trip_visibility import viewer_hidden_activity_ids

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import Trip


def build_trip_map_points(trip: Trip, viewer: Profile, *, include_past: bool = False) -> list[dict[str, Any]]:
    """Build the trip map's marker list for one viewer.

    Two kinds of point come back, distinguishable by ``index``:

    - The trip's own stops, numbered contiguously from 1 in itinerary order
      and ``draggable``. A stop with no coordinates, or whose location this
      viewer may not see, is skipped entirely rather than emitted with null
      coordinates - and skipping it does not consume a number.
    - Ghost markers contributed by a nested child trip, which carry
      ``index: None``, ``activity_id: None``, ``draggable: False`` and an extra
      ``child_trip: True`` key. Their labels are prefixed with the child trip's
      name. Each child trip contributes its markers only once, however many of
      this trip's activities link to it.

    Args:
        trip: The trip being mapped.
        viewer: The profile viewing the map; drives per-activity location
            visibility (see ``trip_visibility.viewer_hidden_activity_ids``).
        include_past: When True, completed activities keep their markers
            instead of being dropped.

    Returns:
        Marker dicts in itinerary order, ready to serialize as-is.
    """
    activities = list(activity_queryset(trip))

    # Activities viewer-hidden due to the adder's privacy setting.
    viewer_hidden_map = viewer_hidden_activity_ids(activities, viewer)

    points: list[dict[str, Any]] = []
    index = 1
    seen_child_acts: set[int] = set()

    for act in activities:
        if act.status == TripActivity.STATUS_COMPLETED and not include_past:
            continue

        coords = activity_coords(act)

        if coords and not act.location_hidden and act.id not in viewer_hidden_map:
            label = act.effective_title
            points.append(
                {
                    "index": index,
                    "activity_id": act.id,
                    "label": label,
                    "lat": coords[0],
                    "lng": coords[1],
                    "status": act.status,
                    "scheduled_at": act.scheduled_at.isoformat() if act.scheduled_at else None,
                    "draggable": True,
                },
            )
            index += 1

        # Include child trip's activities as ghost markers
        child_trip = act.child_trip
        if child_trip is not None and child_trip.id not in seen_child_acts:
            seen_child_acts.add(child_trip.id)
            child_acts = list(activity_queryset(child_trip))
            for child_act in child_acts:
                if child_act.location_hidden:
                    continue
                child_coords = activity_coords(child_act)
                if not child_coords:
                    continue
                child_label = child_act.effective_title
                points.append(
                    {
                        "index": None,
                        "activity_id": None,
                        "label": f"[{child_trip.name}] {child_label}",
                        "lat": child_coords[0],
                        "lng": child_coords[1],
                        "status": child_act.status,
                        "scheduled_at": child_act.scheduled_at.isoformat() if child_act.scheduled_at else None,
                        "draggable": False,
                        "child_trip": True,
                    },
                )

    return points
