"""Wiki eligibility for a Consensus session.

Only wikis whose Location the requesting profile has *visited*-pinned are
ever offered as rounds - mirrors SpotGuessr's "only locations pinned by
every participant" rule (``services.spotguessr.eligibility``), but visited-
only (not just pinned), per the Consensus design spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def eligible_wikis(profile: Profile, *, exclude_wiki_ids: Iterable[int] = ()) -> QuerySet[Wiki]:
    """Wikis whose Location ``profile`` has a visited pin for.

    Args:
        profile: The profile a solo round (or one participant of a
            competitive round) is being generated for.
        exclude_wiki_ids: Wikis to exclude outright - already used earlier
            in this session (no repeats within one playthrough).

    Returns:
        A Wiki queryset, unevaluated.
    """
    visited_location_ids = Pin.objects.by_profile(profile).visited().values_list("location_id", flat=True)
    candidates = Wiki.objects.filter(location_id__in=visited_location_ids)
    exclude_ids = list(exclude_wiki_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)
    return candidates.distinct()


def eligible_wikis_for_all(profiles: Iterable[Profile], *, exclude_wiki_ids: Iterable[int] = ()) -> QuerySet[Wiki]:
    """Wikis every profile in ``profiles`` has visited-pinned - the competitive-mode rule.

    A competitive round's content must be identical for every joined
    participant, so its wiki pool is the intersection of each participant's
    own eligible wikis, not the union.

    Args:
        profiles: Every joined participant of the session.
        exclude_wiki_ids: See ``eligible_wikis``.

    Returns:
        A Wiki queryset, unevaluated. Empty (``.none()``) when ``profiles``
        is empty - there is no sensible "eligible for nobody."
    """
    profiles = list(profiles)
    if not profiles:
        return Wiki.objects.none()

    candidates = Wiki.objects.all()
    for profile in profiles:
        visited_location_ids = Pin.objects.by_profile(profile).visited().values_list("location_id", flat=True)
        candidates = candidates.filter(location_id__in=visited_location_ids)

    exclude_ids = list(exclude_wiki_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)
    return candidates.distinct()


def has_eligible_wikis(profile: Profile) -> bool:
    """Whether ``eligible_wikis`` would return anything at all, without materializing it.

    Used as a cheap pre-check before creating a solo session - a profile
    with no visited pins should never get an ACTIVE session with zero
    possible rounds.
    """
    return eligible_wikis(profile).exists()


def has_eligible_wikis_for_all(profiles: Iterable[Profile]) -> bool:
    """Whether ``eligible_wikis_for_all`` would return anything at all, without materializing it."""
    return eligible_wikis_for_all(profiles).exists()
