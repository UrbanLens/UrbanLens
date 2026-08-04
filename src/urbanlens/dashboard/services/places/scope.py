"""Parcel-or-building scope: what a marker describes, and what it draws.

One rule, in one place, consulted by boundary resolution, every building-level
panel, and the type badge on both detail pages.

A parcel and a building are the same thing for an ordinary house, and calling
it either would be drawing a distinction that isn't there - so a marker on a
single-building property stays neutral and shows both outlines, exactly as it
always has. On a property with several buildings they are emphatically not the
same thing, and a marker has to commit: the campus marker describes the
grounds, a marker on one of its structures describes that structure. That
commitment is what stops a campus page rendering "TOOL SHED (1937)" as if it
were the whole hospital, and what stops a building's page drawing 200 acres of
parcel around a 90-foot footprint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.place.model import PlaceKind

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def pin_type_for_place(place: Place | None) -> str | None:
    """The marker type a place implies, or None when it implies nothing.

    Args:
        place: The resolved place, or None.

    Returns:
        A :class:`~urbanlens.dashboard.models.pin.model.PinType` value, or
        None when the place carries no scope signal and the caller should fall
        back to its own heuristics.
    """
    from urbanlens.dashboard.models.pin.model import PinType

    if place is None:
        return None
    if place.kind == PlaceKind.SITE:
        return PinType.PARCEL
    if not place.is_multi_building:
        # One building on one parcel: the neutral default, which is exactly
        # what it means - this marker is both.
        return PinType.LOCATION_MARKER
    return PinType.BUILDING if place.kind == PlaceKind.BUILDING else PinType.PARCEL


def place_polygon(place: Place | None, boundary_type: str) -> MultiPolygon | None:
    """The outline a place contributes for one boundary type.

    Args:
        place: The resolved place, or None.
        boundary_type: A :class:`~urbanlens.dashboard.models.boundary.model.BoundaryType` value.

    Returns:
        The polygon to draw, or None when this place has nothing to say about
        that boundary type.
    """
    from urbanlens.dashboard.models.boundary.model import BoundaryType

    if place is None:
        return None

    if boundary_type == BoundaryType.BUILDING:
        # Only a marker standing on a footprint has a building. A marker on
        # the grounds of a multi-building site deliberately has none: picking
        # one of its structures would be inventing an answer.
        return place.geometry if place.kind == PlaceKind.BUILDING else None

    if place.kind != PlaceKind.BUILDING:
        return place.geometry
    if place.is_multi_building:
        # A building on a property with others: its page shows the building,
        # not the grounds it shares with 123 more of them.
        return None
    parcel = place.parcel
    return parcel.geometry if parcel is not None else None


def parcel_polygon_for_location(location) -> MultiPolygon | None:
    """The real parcel outline a coordinate stands on, or None.

    "Real" excludes the synthesized fallback circle: counting the buildings
    inside an arbitrary 50 m disc, or nesting every pin within one, would be
    worse than doing nothing. Callers that need a shape to *draw* should use
    ``Boundary.objects.resolve_for_*`` instead, which does fall back.

    Args:
        location: The location to look up; None is tolerated.

    Returns:
        The parcel's official geometry, or None when the coordinate is on no
        known parcel.
    """
    if location is None or not location.place_id or location.place is None:
        return None
    parcel = location.place.parcel
    return parcel.geometry if parcel is not None else None


#: What each scope badge tells a viewer, keyed by
#: :class:`~urbanlens.dashboard.models.pin.model.PinType` value. Only types
#: that say something a viewer can't already see get an entry - the neutral
#: default is absent on purpose, since badging every ordinary house "Location"
#: would be noise.
SCOPE_BADGES: dict[str, tuple[str, str]] = {
    "parcel": ("Parcel", "This page describes the grounds - the buildings on it have their own pages."),
    "building": ("Building", "This page describes one building, not the property it stands on."),
    "entrance": ("Entrance", "A way in, marked on the property it belongs to."),
    "poi": ("Point of Interest", "Something worth noting on the property it belongs to."),
    "danger": ("Hazard", "A hazard marked on the property it belongs to."),
    "other": ("Other", "A marker that doesn't fit the usual categories."),
}


def scope_badge(target: Pin | Wiki) -> dict[str, str]:
    """Badge context for a marker's header, empty when there's nothing to say.

    Args:
        target: The pin or wiki being rendered.

    Returns:
        A dict with ``scope_type``, ``scope_label`` and ``scope_help``, or an
        empty dict for the neutral default (an ordinary property, where
        "parcel" and "building" describe the same thing).
    """
    scope = effective_pin_type(target)
    entry = SCOPE_BADGES.get(scope)
    if entry is None:
        return {}
    label, help_text = entry
    return {"scope_type": scope, "scope_label": label, "scope_help": help_text}


def effective_pin_type(target: Pin | Wiki) -> str:
    """The type a pin or wiki actually reads as, user choice included.

    Args:
        target: The pin or wiki being rendered.

    Returns:
        A :class:`~urbanlens.dashboard.models.pin.model.PinType` value.
    """
    from urbanlens.dashboard.models.pin.model import PinType

    if target.pin_type_is_user_provided:
        return target.pin_type

    place = target.location.place if target.location_id and target.location is not None else None
    if (implied := pin_type_for_place(place)) is not None:
        return implied

    # Placeless: no provider knows this coordinate, so fall back to the shape
    # of the user's own hierarchy - several children typed as buildings still
    # means this marker is describing the grounds they stand on.
    from urbanlens.dashboard.services.locations.site_scope import MULTI_BUILDING_THRESHOLD, building_child_count

    if target.pin_type not in {PinType.PARCEL, PinType.BUILDING, PinType.LOCATION_MARKER}:
        # An entrance or a hazard is not a claim about parcel-vs-building
        # scope, and nothing here should second-guess it.
        return target.pin_type
    if building_child_count(target) >= MULTI_BUILDING_THRESHOLD:
        return PinType.PARCEL
    # A stored PARCEL that nobody chose was a guess, and with no buildings
    # under it there is nothing left supporting the guess - so it doesn't get
    # to decide. A stored BUILDING is different: it was written by
    # ``classify_building_pin_type`` because the marker stands on a footprint,
    # which is an observation rather than a guess.
    return PinType.BUILDING if target.pin_type == PinType.BUILDING else PinType.LOCATION_MARKER
