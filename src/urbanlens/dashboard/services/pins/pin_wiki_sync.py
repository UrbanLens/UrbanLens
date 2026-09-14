"""Bulk, user-initiated sync between a pin's child pins and its wiki's child wikis."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from django.contrib.gis.geos import Point
from django.db import transaction

from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.services.locations import site_scope
from urbanlens.dashboard.services.pins import pin_restructure

if TYPE_CHECKING:
    from decimal import Decimal

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Cap on how many markers one sync call will create, mirroring
#: ``pin_restructure.MAX_RESTRUCTURE_ITEMS`` for the same reason (a backstop,
#: not a realistic ceiling).
MAX_SYNC_ITEMS = 500


class _Located(Protocol):
    """Structural type for anything with coordinates - both Pin and Wiki qualify."""

    @property
    def latitude(self) -> Decimal: ...
    @property
    def longitude(self) -> Decimal: ...
    @property
    def pin_type(self) -> str: ...


def _nearest_uncovered[T: _Located](marker: _Located, candidates: list[T]) -> T | None:
    """The candidate closest to ``marker``, within building-match range - or None.
    The proximity-only fallback: for anything not typed as a building (an entrance, a POI, a hazard - see :func:`_find_existing_match`), or when no REData footprint data settles it.

    Args:
        marker: A pin or wiki to find a match for.
        candidates: Markers of the *other* kind, not yet matched to anything.

    Returns:
        The nearest candidate within ``site_scope.BUILDING_MATCH_METERS``, or None."""
    if not candidates:
        return None
    lat, lng = float(marker.latitude), float(marker.longitude)

    def distance(candidate: _Located) -> float:
        return site_scope.meters_between(float(candidate.latitude), float(candidate.longitude), lat, lng)

    nearest = min(candidates, key=distance)
    return nearest if distance(nearest) <= site_scope.BUILDING_MATCH_METERS else None


def _building_containing(marker: _Located, buildings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first REData building record whose real footprint contains ``marker``'s point.

    Args:
        marker: A pin or wiki to locate.
        buildings: Cached REData building records for the parcel (see ``plugins.builtin.parcel_buildings``); records with no footprint (a bare point, or nothing parseable) never match here.

    Returns:
        The containing building record, or None."""
    point = Point(float(marker.longitude), float(marker.latitude), srid=4326)
    for building in buildings:
        footprint = pin_restructure.building_footprint(building)
        if footprint is not None and (footprint.contains(point) or footprint.touches(point)):
            return building
    return None


def _find_existing_match[T: _Located](marker: _Located, candidates: list[T], buildings: list[dict[str, Any]]) -> T | None:
    """The candidate that already covers ``marker``.

    Args:
        marker: The pin or wiki being matched.
        candidates: Markers of the other kind, not yet matched to anything.
        buildings: The parcel's cached REData building records (``[]`` when none have ever been fetched for this location).

    Returns:
        The matching candidate, or None."""
    if marker.pin_type == PinType.BUILDING and buildings:
        containing = _building_containing(marker, buildings)
        if containing is not None:
            same_building = next(
                (candidate for candidate in candidates if candidate.pin_type == PinType.BUILDING and _building_containing(candidate, buildings) is containing),
                None,
            )
            if same_building is not None:
                return same_building
    return _nearest_uncovered(marker, candidates)


def send_pins_to_wiki(parent_pin: Pin, children: list[Pin], profile: Profile) -> int:
    """Create a matching child wiki for each selected child pin not already covered.
    Never creates the wiki itself - community pages are only ever created explicitly (``services.wiki.wiki_share.WikiShareService``); this silently does nothing when the property has none yet.

    Args:
        parent_pin: The parent pin, whose location's wiki (if any) gains children.
        children: The specific child pins selected to send.
        profile: The profile to attribute the resulting WikiEdit to.

    Returns:
        How many child wikis were created."""
    from urbanlens.dashboard.controllers.detail_pins import ChildWikiLocationError, _location_for_child_wiki
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit

    wiki = Wiki.objects.get_for_location(parent_pin.location)
    if wiki is None:
        return 0

    buildings = site_scope.parcel_buildings(parent_pin.location) or []
    unmatched_wikis = list(wiki.child_wikis.select_related("location"))
    created = 0
    with transaction.atomic():
        for child in children[:MAX_SYNC_ITEMS]:
            existing = _find_existing_match(child, unmatched_wikis, buildings)
            if existing is not None:
                unmatched_wikis.remove(existing)
                continue
            try:
                child_location = _location_for_child_wiki(child.latitude, child.longitude)
            except ChildWikiLocationError:
                # Same collision services.pins.pin_restructure.mirror_buildings_to_wiki already
                # guards against: something (often the parent wiki itself) already occupies that
                # exact point.
                logger.info("send_pins_to_wiki: skipping pin %s - a wiki marker already occupies its point", child.pk)
                continue
            child_wiki = Wiki.objects.create(
                # The pin's owner placed this, by sending their pins to the
                # wiki - see the matching note in controllers/detail_pins.py.
                created_by_id=child.profile_id,
                # A detail pin is never left at the LOCATION_MARKER default -
                # the dialog's Type select excludes it, and auto-classification
                # only ever lands on BUILDING or the POINT_OF_INTEREST fallback.
                name=child.effective_name,
                pin_type=child.pin_type,
                pin_type_is_user_provided=child.pin_type_is_user_provided,
                parent_wiki=wiki,
                location=child_location,
            )
            created += 1
            logger.debug("send_pins_to_wiki: created child wiki %s from pin %s", child_wiki.pk, child.pk)

    if created:
        # One entry for the whole batch - one row per pin would bury every
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"child_wikis_imported": {"from": None, "to": f"{created} marker{'s' if created != 1 else ''} from your child pins"}},
        )
    return created


def pull_children_from_wiki(parent_pin: Pin) -> int:
    """Create a personal child pin for each of the wiki's child wikis not already covered.

    Args:
        parent_pin: The parent pin to add matching child pins under.

    Returns:
        How many child pins were created."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.pins.pin_creation import PinCreationError, resolve_child_pin_location

    wiki = Wiki.objects.get_for_location(parent_pin.location)
    if wiki is None:
        return 0

    child_wikis = list(wiki.child_wikis.select_related("location"))
    if not child_wikis:
        return 0

    buildings = site_scope.parcel_buildings(parent_pin.location) or []
    unmatched_pins = list(parent_pin.detail_pins.select_related("location"))
    created = 0
    with transaction.atomic():
        for cw in child_wikis[:MAX_SYNC_ITEMS]:
            existing = _find_existing_match(cw, unmatched_pins, buildings)
            if existing is not None:
                unmatched_pins.remove(existing)
                continue
            try:
                location = resolve_child_pin_location(parent_pin.profile, cw.latitude, cw.longitude)
            except PinCreationError:
                # The owner already has a pin on that exact point, so this child wiki is in fact
                # already covered - _find_existing_match just didn't recognise it (no building
                # match, and further apart than its proximity threshold).
                # Skip it rather than aborting the whole pull.
                logger.debug("pull_children_from_wiki: child wiki %s already pinned at its exact point", cw.pk)
                continue
            new_pin = Pin.objects.create(
                name=cw.name,
                name_is_user_provided=False,
                pin_type=cw.pin_type,
                pin_type_is_user_provided=cw.pin_type_is_user_provided,
                parent_pin=parent_pin,
                profile=parent_pin.profile,
                location=location,
                wiki=wiki,
            )
            created += 1
            logger.debug("pull_children_from_wiki: created pin %s from child wiki %s", new_pin.pk, cw.pk)
    return created
