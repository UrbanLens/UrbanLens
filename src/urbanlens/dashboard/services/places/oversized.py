"""Taking places too large to be what they claim out of every access domain (P148).

``PlaceQuerySet.implausible`` already keeps such a place from resolving or sharing access. This removes what it
left behind: locations still attached to it, buildings parented under it, wikis nested because of it, and marker
types it implied.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from django.db import transaction
from django.db.models import Q, QuerySet

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceStatus, implausible_area_q
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places import lineage, resolution
from urbanlens.dashboard.services.places.scope import pin_type_for_place

logger = logging.getLogger(__name__)

_SCOPED_PIN_TYPES = (PinType.LOCATION_MARKER, PinType.PARCEL, PinType.BUILDING)


@dataclass(slots=True)
class DetachOutcome:
    """What detaching one place changed.

    Attributes:
        place_id: The detached place.
        children: Places that were its children and now root their own domains.
        locations: Locations that stood in its domain.
        unplaced: Of those, how many now stand on no known place and will be asked about again.
        wikis_unnested: Wikis taken out from under a wiki that stood in its domain.
    """

    place_id: int
    children: int = 0
    locations: int = 0
    unplaced: int = 0
    wikis_unnested: int = 0


def places_to_detach() -> list[Place]:
    """Implausibly large places that still hold anything, largest first.

    Returns:
        The places :func:`detach_oversized_place` would change.
    """
    return [place for place in Place.objects.filter(implausible_area_q()).order_by("-area_sqm", "pk") if _holds_anything(place)]


def locations_standing_on(place: Place) -> QuerySet[Location]:
    """Locations on the place, on its children, or anywhere in a domain it roots.

    Args:
        place: The place.

    Returns:
        The locations.
    """
    return Location.objects.filter(Q(place=place) | Q(place__parent=place) | Q(place__domain_root_id=place.pk)).distinct()


def _holds_anything(place: Place) -> bool:
    return place.status == PlaceStatus.CURRENT or place.children.exists() or locations_standing_on(place).exists() or Wiki.objects.filter(place=place).exists()


@transaction.atomic
def detach_oversized_place(place: Place) -> DetachOutcome:
    """Retire an implausibly large place and release everything that shared access through it.

    Children become their own domain roots rather than being deleted, since a building footprint under a bogus
    parcel is usually still a real building. Locations are re-resolved; those left on no place are marked
    unresolved so the provider chain is asked about them again. Wikis nested under a wiki in the domain are
    un-nested and re-reconciled, so only nesting the repaired places still justify comes back.

    Args:
        place: The place to detach.

    Returns:
        What changed.
    """
    from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting

    outcome = DetachOutcome(place_id=place.pk)
    location_ids = list(locations_standing_on(place).values_list("pk", flat=True))
    domain_wikis = list(Wiki.objects.filter(Q(location_id__in=location_ids) | Q(place=place)).values_list("pk", flat=True))

    Place.objects.filter(pk=place.pk).update(status=PlaceStatus.SUPERSEDED)
    for child in list(place.children.all()):
        lineage.set_parent(child, None)
        outcome.children += 1
    Wiki.objects.filter(place=place).update(place=None)

    for location in Location.objects.filter(pk__in=location_ids).select_related("place"):
        resolution.resolve_location_place(location)
        implied = pin_type_for_place(location.place) or PinType.LOCATION_MARKER
        Pin.objects.filter(location=location, pin_type_is_user_provided=False, pin_type__in=_SCOPED_PIN_TYPES).exclude(pin_type=implied).update(pin_type=implied)
        Wiki.objects.filter(location=location, pin_type_is_user_provided=False, pin_type__in=_SCOPED_PIN_TYPES).exclude(pin_type=implied).update(pin_type=implied)
    outcome.locations = len(location_ids)
    outcome.unplaced = Location.objects.filter(pk__in=location_ids, place__isnull=True).update(place_resolved_at=None)

    unnested = list(Wiki.objects.filter(parent_wiki_id__in=domain_wikis).values_list("pk", flat=True))
    Wiki.objects.filter(pk__in=unnested).update(parent_wiki=None)
    outcome.wikis_unnested = len(unnested)
    for wiki in Wiki.objects.filter(pk__in=[*unnested, *domain_wikis]):
        reconcile_wiki_nesting(wiki)

    logger.info("Detached implausible place %s (%.1f km²): %s", place.pk, (place.area_sqm or 0) / 1_000_000, outcome)
    return outcome
