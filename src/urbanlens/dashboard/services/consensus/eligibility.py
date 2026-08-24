"""Wiki eligibility for a Consensus session.

Only wikis whose Location the requesting profile has *visited*-pinned are
ever offered as rounds - mirrors SpotGuessr's "only locations pinned by
every participant" rule (``services.spotguessr.eligibility``), but visited-
only (not just pinned), per the Consensus design spec.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Prefetch

from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile


def _visible_images_prefetch(profiles: list[Profile], candidates: QuerySet[Wiki]) -> Prefetch:
    """Prefetch each wiki's images, filtered to what every participant may see.

    Being eligible for a wiki is only the container gate: a visited pin grants
    the wiki, and says nothing about whether each uploader's
    ``photo_upload_visibility`` admits this player to the photos on it. Applying
    that here means every strategy reading ``wiki.images.all()`` gets a list it
    is allowed to show, rather than each having to remember.

    For a competitive session the filters chain, so the result is the
    intersection - the same rule the wiki pool itself follows, because a round's
    content has to be identical for everyone in it.

    Args:
        profiles: Every joined participant.
        candidates: The wiki queryset being prefetched, used to narrow the image
            set before ``visible_to`` resolves - it is eager, and resolving it
            against the unfiltered manager would inspect every uploader on the
            site.

    Returns:
        A ``Prefetch`` for the ``images`` relation.
    """
    from urbanlens.dashboard.models.images.model import Image

    images = Image.objects.filter(wiki_id__in=candidates.values("pk"))
    for profile in profiles:
        images = images.visible_to(profile)
    return Prefetch("images", queryset=images)


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
    candidates = Wiki.objects.official().filter(location_id__in=visited_location_ids)
    exclude_ids = list(exclude_wiki_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)
    # The field strategies read wiki.aliases and wiki.images once per wiki, and
    # selection calls them once per field kind - so without these prefetches the
    # cost is kinds x wikis x relations. Only .all() reads the cache, which is why
    # the strategies in fields.py must not use .count()/.exists()/.filter() here -
    # and now that the images prefetch is the visibility filter, a .filter() there
    # would skip the gate as well as the cache.
    return candidates.prefetch_related("aliases", _visible_images_prefetch([profile], candidates)).distinct()


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

    candidates = Wiki.objects.official()
    for profile in profiles:
        visited_location_ids = Pin.objects.by_profile(profile).visited().values_list("location_id", flat=True)
        candidates = candidates.filter(location_id__in=visited_location_ids)

    exclude_ids = list(exclude_wiki_ids)
    if exclude_ids:
        candidates = candidates.exclude(pk__in=exclude_ids)
    # See eligible_wikis for why these prefetches exist and why the strategies
    # must read them with .all(). The image filter is the intersection across
    # participants, matching the wiki pool above.
    return candidates.prefetch_related("aliases", _visible_images_prefetch(profiles, candidates)).distinct()


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
