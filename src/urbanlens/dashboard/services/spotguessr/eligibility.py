"""Location eligibility for a SpotGuessr session.

See ``docs/designs/spotguessr.md`` ("Eligibility") - the one rule repeated
for every mode: only locations pinned by *every* participant are ever
offered, including a solo session's one player.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.model import Label
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
    label_id: int | None = None,
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
        label_id: Optional ``Label`` id (``config.label_id``) restricting candidates to
            locations where at least one participant's own pin carries that label or
            one of its descendants (``Label.get_label_and_descendants``). Applied as an
            *additional* narrowing condition scoped to ``pins__profile__in=profiles`` -
            it can only shrink the already-pinned-by-everyone pool above, never surface a
            location some participant hasn't pinned themselves.

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

    if label_id is not None:
        expanded_label_ids = Label.get_label_and_descendants(label_id)
        candidates = candidates.filter(pins__profile__in=profiles, pins__labels__id__in=expanded_label_ids)

    if geo_bounds is not None:
        candidates = candidates.filter(point__within=geo_bounds)

    exclude_ids = list(exclude_location_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)

    return candidates.distinct()


def has_eligible_locations(
    profiles: Iterable[Profile],
    *,
    require_visited_by_all: bool = False,
    geo_bounds: GEOSGeometry | None = None,
    label_id: int | None = None,
) -> bool:
    """Whether ``eligible_locations`` would return anything at all, without materializing it.

    Used as a cheap pre-check before creating a solo session - a profile
    with no pins (or whose pins all fall outside a chosen ``geo_bounds``)
    should never get an ACTIVE session with zero possible rounds; see
    ``controllers.spotguessr.SpotGuessrStartView`` for how this replaces
    the old "create a session, then discover it can't play, then fake a
    completed summary" flow.

    Args:
        profiles: Every participant in the session.
        require_visited_by_all: See ``eligible_locations``.
        geo_bounds: See ``eligible_locations``.
        label_id: See ``eligible_locations``.

    Returns:
        True if at least one location is eligible for every profile.
    """
    return eligible_locations(
        profiles,
        require_visited_by_all=require_visited_by_all,
        geo_bounds=geo_bounds,
        label_id=label_id,
    ).exists()
