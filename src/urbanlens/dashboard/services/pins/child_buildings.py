"""A property's building child pins, surfaced on the property's own Private Pin page.

A building's own records (its CRIS entry, characteristics, notes, description) live on its child pin. The page-wide
"child pin details" toggle brings them up to the property's page: a lone building is shown in full, a campus shows the
building the pin stands in and lists the rest collapsed, each loading only when opened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point

from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.place.model import PlaceKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import InfoPanelSource

#: Collapsed building rows per request; the rest arrive through "Show more".
CHILD_BUILDINGS_PAGE_SIZE = 20


def building_children(pin: Pin) -> QuerySet[Pin]:
    """The pin's direct children typed as buildings.

    Args:
        pin: The property pin.

    Returns:
        The building child pins, with what their cards read already joined.
    """
    return pin.detail_pins.filter(pin_type=PinType.BUILDING).select_related("location", "location__place", "profile")


def child_details_default(pin: Pin, building_count: int) -> bool:
    """Whether the page-wide child-details toggle starts on for this pin.

    Args:
        pin: The pin whose page is being rendered.
        building_count: How many of its direct children are buildings.

    Returns:
        True for a parcel, whose children are its content, and for any property holding exactly one building, whose
        records would otherwise sit out of sight on the child. False for a building with a structure inside it.
    """
    from urbanlens.dashboard.services.locations.site_scope import is_site_scope
    from urbanlens.dashboard.services.places.scope import effective_pin_type

    if is_site_scope(pin):
        return True
    return building_count == 1 and effective_pin_type(pin) != PinType.BUILDING


def building_holding(pin: Pin, buildings: Sequence[Pin]) -> Pin | None:
    """The building child the pin itself stands in, if any.

    Args:
        pin: The property pin.
        buildings: Its building children.

    Returns:
        The building whose footprint contains the pin's point, else the nearest one within
        ``BUILDING_MATCH_METERS``, else None.
    """
    from urbanlens.dashboard.services.locations.site_scope import BUILDING_MATCH_METERS, meters_between

    if pin.location_id is None:
        return None
    latitude, longitude = pin.effective_latitude, pin.effective_longitude
    point = Point(longitude, latitude, srid=4326)
    for building in buildings:
        place = building.location.place if building.location_id and building.location.place_id else None
        if place is not None and place.kind == PlaceKind.BUILDING and place.geometry is not None and place.geometry.contains(point):
            return building

    nearest: Pin | None = None
    nearest_distance = BUILDING_MATCH_METERS
    for building in buildings:
        if building.location_id is None:
            continue
        distance = meters_between(building.effective_latitude, building.effective_longitude, latitude, longitude)
        if distance <= nearest_distance:
            nearest, nearest_distance = building, distance
    return nearest


@dataclass(frozen=True, slots=True)
class ChildBuildingListing:
    """What the property page shows of its building children.

    Attributes:
        expanded: The building shown in full: the only one, or the one the pin stands in.
        rows: One page of the remaining buildings, collapsed.
        total_rows: How many buildings the collapsed list holds across every page.
        next_offset: Where the next page starts, or None on the last page.
    """

    expanded: Pin | None
    rows: list[Pin] = field(default_factory=list)
    total_rows: int = 0
    next_offset: int | None = None


def child_building_listing(pin: Pin, *, offset: int = 0, page_size: int = CHILD_BUILDINGS_PAGE_SIZE) -> ChildBuildingListing | None:
    """Arrange a property's building children for its page.

    Args:
        pin: The property pin.
        offset: Where in the collapsed list this page starts.
        page_size: Collapsed rows per page.

    Returns:
        The listing, or None when the pin has no building children.
    """
    buildings = sorted(building_children(pin), key=lambda building: (building.effective_name.casefold(), building.pk))
    if not buildings:
        return None
    expanded = buildings[0] if len(buildings) == 1 else building_holding(pin, buildings)
    rest = [building for building in buildings if building is not expanded]
    offset = max(offset, 0)
    end = offset + page_size
    return ChildBuildingListing(
        expanded=expanded,
        rows=rest[offset:end],
        total_rows=len(rest),
        next_offset=end if end < len(rest) else None,
    )


def building_panel_sources(user: AbstractBaseUser | AnonymousUser) -> list[InfoPanelSource]:
    """The info panels that describe one structure, as the viewer may see them.

    Args:
        user: The viewer.

    Returns:
        Every ``building_level`` info panel the viewer holds the feature for, in registry order.
    """
    from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, panel_sources, panel_visible_to

    return [source for source in panel_sources().values() if isinstance(source, InfoPanelSource) and source.building_level and panel_visible_to(user, source)]
