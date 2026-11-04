"""
One primary wiki per pin, chosen automatically (ADR34 section 5).

``Pin.subject_place`` records what a pin is *about*; this module turns it into the wiki the pin page leads with.
The subject never grants access.
It only picks among wikis the viewer can already open, so every candidate passes ``wiki_access.wiki_accessible_to`` before it can be primary or related.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.places.lineage import ancestors_of
from urbanlens.dashboard.services.wiki.wiki_access import wiki_accessible_to

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The most "related" wikis a pin page lists, so a big campus cannot make the page unbounded.
MAX_RELATED_WIKIS = 25


@dataclass(frozen=True)
class PinWikis:
    """A pin's primary wiki and the other wikis its viewer can reach in the same domain."""

    primary: Wiki | None = None
    related: list[Wiki] = field(default_factory=list)


def current_place_id_for_location(location_id: int) -> int | None:
    """
    The place a Location resolves to right now.

    Args:
        location_id: Primary key of the Location.

    Returns:
        The place's primary key, or None when the Location is on no known place."""
    return Location.objects.filter(pk=location_id).values_list("place_id", flat=True).first()


def _effective_place(pin: Pin) -> Place | None:
    """The pin's subject, else the place its Location resolves to now."""
    if pin.subject_place_id is not None:
        return pin.subject_place
    if pin.location_id is not None and pin.location.place_id is not None:
        return pin.location.place
    return None


def primary_wiki_for_pin(pin: Pin, viewer: Profile) -> PinWikis:
    """
    Choose the pin's primary wiki and its related wikis for *viewer*.

    The primary wiki is the first of these that exists and that *viewer* may open: the subject place's wiki, the nearest ancestor's wiki, the wiki at the pin's exact point (placeless pins).
    Everything else the viewer can reach in the pin's domain is related.

    Args:
        pin: The pin whose page is being built.
        viewer: Who is looking; nothing they cannot already open is returned.

    Returns:
        The primary wiki (None when there is none) and the related wikis, never including the primary."""
    place = _effective_place(pin)
    accessible: dict[int, bool] = {}

    def _usable(wiki: Wiki | None) -> bool:
        if wiki is None:
            return False
        if wiki.pk not in accessible:
            accessible[wiki.pk] = wiki_accessible_to(wiki, viewer)
        return accessible[wiki.pk]

    exact = Wiki.objects.get_for_location(pin.location) if pin.location_id else None
    candidates: list[Wiki | None] = []
    if place is not None:
        # A campus building with no Place of its own is a finer subject than the parcel it stands on: its wiki is found by
        # position (``building_wikis``), has no ``place``, and would otherwise lose to the campus wiki.
        if exact is not None and exact.place_id is None and exact.location.place_id == place.pk:
            candidates.append(exact)
        candidates.append(Wiki.objects.filter(place_id=place.pk).first())
        candidates.extend(Wiki.objects.filter(place_id=ancestor.pk).first() for ancestor in ancestors_of(place))
    else:
        candidates.append(exact)
    primary = next((wiki for wiki in candidates if _usable(wiki)), None)

    related: list[Wiki] = []
    if place is not None and place.domain_root_id is not None:
        in_domain: Iterable[Wiki] = Wiki.objects.filter(place__domain_root_id=place.domain_root_id, parent_wiki__isnull=True).select_related("location", "place").order_by("pk")[: MAX_RELATED_WIKIS + 1]
        related = [wiki for wiki in in_domain if wiki != primary and _usable(wiki)]
    if exact is not None and exact != primary and exact not in related and _usable(exact):
        related.append(exact)
    return PinWikis(primary=primary, related=related[:MAX_RELATED_WIKIS])


def repoint_subjects(predecessor: Place, successors: Iterable[Place]) -> int:
    """
    Move pins whose subject was *predecessor* onto the successor containing each pin's point.

    The hook for the lineage-event handler (UL-358); nothing in this story calls it.
    A pin whose point falls in no successor keeps its subject, so history stays about what it was about.

    Args:
        predecessor: The superseded place.
        successors: The places that replaced it.

    Returns:
        How many pins were repointed."""
    moved = 0
    for successor in successors:
        if successor.geometry is None or successor.pk == predecessor.pk:
            continue
        pins = Pin.objects.filter(subject_place=predecessor, location__point__within=successor.geometry)
        moved += pins.update(subject_place=successor)
    return moved


def adopt_subject_for_location(location_id: int, place_id: int | None) -> int:
    """
    Give pins on a Location that just resolved a place their subject, if they have none.

    Args:
        location_id: The Location that gained *place_id*.
        place_id: The place it now resolves to; None is a no-op.

    Returns:
        How many pins were set."""
    if place_id is None:
        return 0
    return Pin.objects.filter(location_id=location_id, subject_place__isnull=True).update(subject_place_id=place_id)
