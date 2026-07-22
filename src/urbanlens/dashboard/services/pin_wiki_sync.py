"""Bulk, user-initiated sync between a pin's child pins and its wiki's child wikis.

Two hierarchies can drift apart even when both exist: someone hand-places a
child pin for a building nobody has documented on the community wiki yet, or
the wiki already has a child wiki for a building the pin owner hasn't gotten
around to pinning personally. Neither side should have to notice and
re-create the other's work by hand - this module is the explicit "sync them"
action reachable from the detail-pins panel's multi-select toolbar (send
selected child pins to the wiki) and its "pull from wiki" button (create
personal child pins for whatever the wiki already has).

This is deliberately a manual, opt-in action rather than automatic background
sync: a child pin a user placed can be private/exploratory in a way they may
not want published, and the community wiki's child wikis may include entries
the pin owner disagrees with. Compare with ``services.pin_restructure``, which
covers the *external-data-driven* case (buildings REData/Overpass already
know about) as part of the one-time "organize this property?" suggestion.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.locations import site_scope

if TYPE_CHECKING:
    from decimal import Decimal

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Cap on how many markers one sync call will create, mirroring
#: ``pin_restructure.MAX_RESTRUCTURE_ITEMS`` for the same reason (a backstop,
#: not a realistic ceiling).
MAX_SYNC_ITEMS = 500


class _Located(Protocol):
    """Structural type for anything with coordinates - both Pin and Wiki qualify.

    Both inherit ``latitude``/``longitude`` from ``abstract.AddressableModel``
    (a proxy to ``self.location``); Pin additionally has its own
    ``effective_latitude``/``effective_longitude`` (a legacy duplicate, see
    that property's own "TODO: Delete this"), which Wiki never grew - so this
    module deliberately uses the property both share.
    """

    @property
    def latitude(self) -> Decimal: ...
    @property
    def longitude(self) -> Decimal: ...


def _nearest_uncovered[T: _Located](marker: _Located, candidates: list[T]) -> T | None:
    """The candidate closest to ``marker``, within building-match range - or None.

    Neither a child pin nor a child wiki publishes a footprint polygon the way
    a REData building record can, so this is proximity-only (unlike
    ``pin_restructure.marker_covers_building``).

    Args:
        marker: A pin or wiki to find a match for.
        candidates: Markers of the *other* kind, not yet matched to anything.

    Returns:
        The nearest candidate within ``site_scope.BUILDING_MATCH_METERS``, or None.
    """
    if not candidates:
        return None
    lat, lng = float(marker.latitude), float(marker.longitude)

    def distance(candidate: _Located) -> float:
        return site_scope.meters_between(float(candidate.latitude), float(candidate.longitude), lat, lng)

    nearest = min(candidates, key=distance)
    return nearest if distance(nearest) <= site_scope.BUILDING_MATCH_METERS else None


def send_pins_to_wiki(parent_pin: Pin, children: list[Pin], profile: Profile) -> int:
    """Create a matching child wiki for each selected child pin not already covered.

    Never creates the wiki itself - community pages are only ever created
    explicitly (``services.locations.creation.WikiCreationService``); this
    silently does nothing when the property has none yet.

    Args:
        parent_pin: The parent pin, whose location's wiki (if any) gains children.
        children: The specific child pins selected to send.
        profile: The profile to attribute the resulting WikiEdit to.

    Returns:
        How many child wikis were created.
    """
    from urbanlens.dashboard.controllers.detail_pins import _location_for_child_wiki
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit

    wiki = Wiki.objects.get_for_location(parent_pin.location)
    if wiki is None:
        return 0

    unmatched_wikis = list(wiki.child_wikis.select_related("location"))
    created = 0
    with transaction.atomic():
        for child in children[:MAX_SYNC_ITEMS]:
            existing = _nearest_uncovered(child, unmatched_wikis)
            if existing is not None:
                unmatched_wikis.remove(existing)
                continue
            child_wiki = Wiki.objects.create(
                # A detail pin is never left at the LOCATION_MARKER default -
                # the dialog's Type select excludes it, and auto-classification
                # only ever lands on BUILDING or the POINT_OF_INTEREST fallback.
                name=child.effective_name,
                pin_type=child.pin_type,
                pin_type_is_user_provided=child.pin_type_is_user_provided,
                parent_wiki=wiki,
                location=_location_for_child_wiki(child.latitude, child.longitude),
            )
            created += 1
            logger.debug("send_pins_to_wiki: created child wiki %s from pin %s", child_wiki.pk, child.pk)

    if created:
        # One entry for the whole batch - one row per pin would bury every
        # other edit in the wiki's history on a large send.
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"child_wikis_imported": {"from": None, "to": f"{created} marker{'s' if created != 1 else ''} from your sub pins"}},
        )
    return created


def pull_children_from_wiki(parent_pin: Pin) -> int:
    """Create a personal child pin for each of the wiki's child wikis not already covered.

    The inverse of :func:`send_pins_to_wiki`: fills in whatever the community
    has documented that this owner hasn't personally pinned yet.

    Args:
        parent_pin: The parent pin to add matching child pins under.

    Returns:
        How many child pins were created.
    """
    from urbanlens.dashboard.controllers.detail_pins import _location_for_coords
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.get_for_location(parent_pin.location)
    if wiki is None:
        return 0

    child_wikis = list(wiki.child_wikis.select_related("location"))
    if not child_wikis:
        return 0

    unmatched_pins = list(parent_pin.detail_pins.select_related("location"))
    created = 0
    with transaction.atomic():
        for cw in child_wikis[:MAX_SYNC_ITEMS]:
            existing = _nearest_uncovered(cw, unmatched_pins)
            if existing is not None:
                unmatched_pins.remove(existing)
                continue
            new_pin = Pin.objects.create(
                name=cw.name,
                name_is_user_provided=False,
                pin_type=cw.pin_type,
                pin_type_is_user_provided=cw.pin_type_is_user_provided,
                parent_pin=parent_pin,
                profile=parent_pin.profile,
                location=_location_for_coords(cw.latitude, cw.longitude),
                wiki=wiki,
            )
            created += 1
            logger.debug("pull_children_from_wiki: created pin %s from child wiki %s", new_pin.pk, cw.pk)
    return created
