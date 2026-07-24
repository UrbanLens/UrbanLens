"""Location eligibility for a SpotGuessr session.

See ``docs/designs/spotguessr.md`` ("Eligibility") - the one rule repeated
for every mode: only locations pinned by *every* participant are ever
offered, including a solo session's one player.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.location.model import Location

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.contrib.gis.geos import GEOSGeometry
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def eligible_locations(
    profiles: Iterable[Profile],
    *,
    require_visited_by_all: bool = False,
    geo_bounds: GEOSGeometry | None = None,
    exclude_location_ids: Iterable[int] = (),
) -> QuerySet[Location]:
    """Locations every profile in ``profiles`` has pinned (and optionally visited).

    Args:
        profiles: Every participant in the session.
        require_visited_by_all: When True, additionally require a
            ``PinVisit`` against each participant's own pin at the location
            (``config.require_visited_all`` - default off).
        geo_bounds: Optional polygon/bbox restricting candidates to a
            player-chosen region.
        exclude_location_ids: Locations to exclude outright - already used
            earlier in this session (no repeats within one playthrough).

    Returns:
        A Location queryset, unevaluated. Empty (``.none()``) when
        ``profiles`` is empty - there is no sensible "eligible for nobody."
    """
    profiles = list(profiles)
    if not profiles:
        return Location.objects.none()

    candidates = Location.objects.all()
    for profile in profiles:
        if require_visited_by_all:
            candidates = candidates.filter(pins__profile=profile, pins__visit_history__isnull=False)
        else:
            candidates = candidates.filter(pins__profile=profile)

    if geo_bounds is not None:
        candidates = candidates.filter(point__within=geo_bounds)

    exclude_ids = list(exclude_location_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)

    return candidates.distinct()
