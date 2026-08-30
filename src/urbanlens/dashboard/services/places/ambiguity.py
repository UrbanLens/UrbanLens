"""When a coordinate genuinely could mean two different places.

This used to fire constantly. Official geometry hung off each Location, fetched
by point lookup, so importing 124 buildings onto one campus gave 124 Locations
their own copy of the same parcel outline - and every visitor was told that 124
other locations covered their pin. They did not: they were the same property,
listed 125 times.

Resolution onto a single Place removes that entire class of report. A parcel
and the buildings on it share one access domain and answer as one thing, so
what survives here is only the real case: two *unrelated* parcels whose county
geometry overlaps, where a coordinate really is ambiguous and the user may want
the other one.

Two deliberate restrictions on what gets listed:

- **Only places in a different access domain.** Everything inside one property
  is the same answer, never a competing one.
- **Only wikis the viewer can already see.** A competing parcel the viewer has
  not earned is not named, not counted, and does not render - naming it would
  disclose a place they have not found. They can still reach it the ordinary
  way, by pinning a coordinate that resolves to it. This trades a nudge in a
  rare case for the guarantee that the notice can never be an oracle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)


def competing_places(latitude, longitude, resolved: Place | None) -> list[Place]:
    """Places that genuinely compete for a coordinate.

    Args:
        latitude: WGS-84 latitude; None is tolerated.
        longitude: WGS-84 longitude; None is tolerated.
        resolved: The place the coordinate resolved onto.

    Returns:
        Competing places, most specific first. Almost always empty.
    """
    from urbanlens.dashboard.models.place.model import Place

    return list(Place.objects.competing_for_point(latitude, longitude, resolved=resolved))


def representative_locations(places) -> list[Location]:
    """One Location per place, for surfaces whose API speaks in locations.

    Prefers the place's wiki location, since that is the one a user would
    recognise; falls back to any location resolved onto it. A place nobody has
    pinned yet has none and is simply omitted - there would be nothing to
    switch a pin to.

    Args:
        places: The places to represent.

    Returns:
        One Location per place that has one, in the order given.
    """
    from urbanlens.dashboard.models.location.model import Location

    chosen: list[Location] = []
    for place in places:
        location = Location.objects.filter(place=place, wiki__isnull=False).first() or Location.objects.filter(place=place).first()
        if location is not None:
            chosen.append(location)
    return chosen


def _shares_lineage(a: Place, b: Place) -> bool:
    """Whether one of two places is a ``PART_OF`` ancestor of the other.

    Two places already sharing a domain root are filtered out by
    ``competing_for_point`` itself. This catches the case a data defect can
    still produce - a parcel and one of its own buildings recorded with
    mismatched ``domain_root`` - so a broken edge can surface a pin's own
    parcel as something to "switch" to, rather than merely fail to grant the
    access the edge should have.

    Args:
        a: One place.
        b: The other place.

    Returns:
        True when they are the same place, or one is an ancestor of the other.
    """
    from urbanlens.dashboard.services.places import lineage

    if a.pk == b.pk:
        return True
    if any(ancestor.pk == b.pk for ancestor in lineage.ancestors_of(a)):
        return True
    return any(ancestor.pk == a.pk for ancestor in lineage.ancestors_of(b))


def competing_wiki_locations(pin, profile: Profile) -> list[Location]:
    """Locations a pin could plausibly be relinked to instead, if any.

    Args:
        pin: The viewer's own pin, whose coordinate is being checked.
        profile: The viewer, so nothing they haven't earned is named.

    Returns:
        One Location per competing place, ready to render as switch targets.
        Empty in every ordinary case - in particular, whenever the pin's own
        coordinate has no resolved place to compare rivals against, since
        nothing can be said to compete with an unknown answer.
    """
    from urbanlens.dashboard.services.wiki.wiki_access import accessible_domain_ids

    if pin is None or pin.location_id is None or not pin.location.place_id:
        return []
    resolved = pin.location.place
    latitude, longitude = pin.effective_latitude, pin.effective_longitude
    rivals = [place for place in competing_places(latitude, longitude, resolved) if not _shares_lineage(place, resolved)]
    if not rivals:
        return []

    visible_domains = accessible_domain_ids(profile)
    wanted = [place for place in rivals if place.domain_root_id in visible_domains]
    if not wanted:
        return []
    # One Location per place - representative_locations picks a single
    # representative rather than every Location that resolves onto it, which
    # is what stops one rival place from exploding into many rows. A
    # wiki-bearing Location is expected to always carry a routing slug; drop
    # any that don't rather than link to a "None" wiki URL.
    return [location for location in representative_locations(wanted) if location.slug]
