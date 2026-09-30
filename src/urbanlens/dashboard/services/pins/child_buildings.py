"""A property's building child pins, surfaced on the property's own Private Pin page.

A building's own records (its CRIS entry, characteristics, notes, description) live on its child pin. The property's
Buildings list opens a pinned building's card in place (``controllers.child_buildings``), and the page-wide "child pin
details" toggle brings the children's photos, visits and comments up to the property's page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.pin.model import PinType

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser, AnonymousUser
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.external_data import InfoPanelSource


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
        records would otherwise sit out of sight on the child. False when the owner typed the pin itself as a
        building, making its one building child a structure inside it.
    """
    from urbanlens.dashboard.services.locations.site_scope import is_site_scope

    if is_site_scope(pin):
        return True
    return building_count == 1 and not (pin.pin_type_is_user_provided and pin.pin_type == PinType.BUILDING)


def building_panel_sources(user: AbstractBaseUser | AnonymousUser) -> list[InfoPanelSource]:
    """The info panels that describe one structure, as the viewer may see them.

    Args:
        user: The viewer.

    Returns:
        Every ``building_level`` info panel the viewer holds the feature for, in registry order.
    """
    from urbanlens.dashboard.services.pins.external_data import InfoPanelSource, panel_sources, panel_visible_to

    return [source for source in panel_sources().values() if isinstance(source, InfoPanelSource) and source.building_level and panel_visible_to(user, source)]
