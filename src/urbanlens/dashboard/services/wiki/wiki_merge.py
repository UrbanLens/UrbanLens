"""Automatic parent/child nesting between community Wikis, following place lineage.
This merge should happen "without needing user confirmation"."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def wiki_property_polygon(wiki: Wiki):
    """The wiki's real property boundary, or None when only the fallback circle exists.

    Args:
        wiki: The wiki whose property boundary to resolve.

    Returns:
        A real (community-drawn or provider-generated) property polygon, or None - also when it is too large to be
        a parcel, which would nest wikis a county apart (P148)."""
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
    from urbanlens.dashboard.models.place.model import PlaceKind, is_plausible_area
    from urbanlens.dashboard.services.geo.area import area_sqm

    polygon, source = Boundary.objects.resolve_for_wiki(wiki, BoundaryType.PROPERTY)
    if polygon is None or source == "circle" or not is_plausible_area(PlaceKind.PARCEL, area_sqm(polygon)):
        return None
    return polygon


def _nestable_child_wikis(wiki: Wiki):
    """Root wikis that belong under this one.
    Only direct children - a grandchild attaches to its own parent when that level reconciles, which is what keeps the tree a tree rather than flattening every descendant onto the outermost ancestor.

    Args:
        wiki: The candidate parent.

    Returns:
        A list of candidate child :class:`Wiki` rows."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    if wiki.place_id is not None:
        return list(Wiki.objects.filter(parent_wiki__isnull=True, place__parent_id=wiki.place_id).exclude(pk=wiki.pk).select_related("location", "place"))

    location_place_id = wiki.location.place_id if wiki.location_id else None
    if location_place_id is not None and Wiki.objects.filter(place_id=location_place_id).exclude(pk=wiki.pk).exists():
        # A campus building's wiki stands on the campus's place, whose outline is the campus wiki's to nest by.
        return []

    polygon = wiki_property_polygon(wiki)
    if polygon is None:
        return []
    return list(
        Wiki.objects.filter(parent_wiki__isnull=True, place__isnull=True, location__point__within=polygon).exclude(pk=wiki.pk).select_related("location", "place"),
    )


def _containing_root_wiki(wiki: Wiki) -> Wiki | None:
    """The wiki of the nearest ancestor place that has one.

    Args:
        wiki: The wiki looking for a bigger container.

    Returns:
        The nearest ancestor's wiki, or None."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.places.lineage import ancestors_of

    if wiki.place_id is None or wiki.place is None:
        return _containing_root_wiki_by_geometry(wiki)

    ancestor_ids = [ancestor.pk for ancestor in ancestors_of(wiki.place)]
    if not ancestor_ids:
        return None
    by_place = {candidate.place_id: candidate for candidate in Wiki.objects.filter(place_id__in=ancestor_ids).select_related("location", "place")}
    for place_id in ancestor_ids:
        if (candidate := by_place.get(place_id)) is not None:
            return candidate
    return None


def _containing_root_wiki_by_geometry(wiki: Wiki) -> Wiki | None:
    """The nearest wiki on the place a placeless wiki's point stands on, or on a place containing that one.

    A building's wiki is placeless when its point resolves onto its parcel, which the parcel's wiki already holds,
    and a building place with no wiki of its own still sits ``PART_OF`` its parcel. On a campus, a wiki standing on
    one of its buildings that has no other wiki is that building's, and nests under the building enclosing it, if
    that has a wiki, before the campus's (``building_wikis.standing_building``).

    Args:
        wiki: The placeless wiki looking for a container.

    Returns:
        The container, or None."""
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.places.lineage import ancestors_of
    from urbanlens.dashboard.services.wiki.building_wikis import standing_building

    location = wiki.location if wiki.location_id else None
    if location is None or location.point is None:
        return None
    place = location.place if location.place_id else Place.objects.resolve_for_point(location.latitude, location.longitude)
    if place is None:
        return None
    chain = [place, *ancestors_of(place)]
    by_place = {candidate.place_id: candidate for candidate in Wiki.objects.filter(place_id__in=[link.pk for link in chain]).exclude(pk=wiki.pk).select_related("location", "place")}
    container = next((by_place[link.pk] for link in chain if link.pk in by_place), None)
    if container is None:
        return None
    building = standing_building(location, container)
    return building.parent if building is not None and building.wiki is None else container


def absorb_wiki(parent: Wiki, child: Wiki) -> None:
    """Nest ``child`` under ``parent`` and log it on the parent's edit history.

    A Wikipedia article seeded while the child was a root, and untouched since, goes when the child turns out to be
    one of the parent's buildings (``wiki_seed.drop_misplaced_wikipedia_seed``).

    Args:
        parent: The wiki that gains a child.
        child: The wiki being nested."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit
    from urbanlens.dashboard.services.wiki.wiki_seed import drop_misplaced_wikipedia_seed

    Wiki.objects.filter(pk=child.pk).update(parent_wiki=parent)
    child.parent_wiki = parent
    WikiEdit.objects.create(
        wiki=parent,
        editor=None,
        changes={"child_wiki_merged": {"from": None, "to": child.name}},
    )
    logger.info("wiki_merge: nested wiki %s (%r) under %s (%r)", child.pk, child.name, parent.pk, parent.name)
    drop_misplaced_wikipedia_seed(child)


def reconcile_wiki_nesting(wiki: Wiki) -> int:
    """Automatically nest this wiki under a bigger one, and absorb smaller ones into it.

    Args:
        wiki: The wiki whose place was just (re)resolved.

    Returns:
        How many wikis were newly nested (0, 1, or more - this wiki plus however many it absorbed)."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    # Re-fetched rather than trusted from the caller: direction 1's guard below reads
    # parent_wiki_id, and an in-memory object holding a since-superseded value (e.g. set moments
    # earlier by a *different* wiki's own reconciliation absorbing this one) would otherwise re-run
    # that search and silently overwrite a just-established, tighter parent with an outer one.
    wiki = Wiki.objects.select_related("location", "place", "place__parent").get(pk=wiki.pk)
    merged = 0

    if wiki.parent_wiki_id is None:
        parent = _containing_root_wiki(wiki)
        if parent is not None and parent.pk != wiki.pk and not wiki.would_create_cycle(parent):
            absorb_wiki(parent, wiki)
            merged += 1

    lineage = Wiki.objects.lineage_ids(wiki)
    for candidate in _nestable_child_wikis(wiki):
        if candidate.pk in lineage:
            continue
        absorb_wiki(wiki, candidate)
        merged += 1

    return merged


def claim_parcel_for_location_wiki(location: Location) -> bool:
    """Give the placeless top-level wiki standing at a location the parcel the location now stands on.

    A wiki made before its location's boundary arrived is created placeless. Until it holds its parcel it is not the
    property's page: a pin elsewhere on the parcel gets a wiki of its own, and the campus's building wikis are never
    given out (``tasks.ensure_building_wikis``). A parcel another wiki already holds is left to nesting.

    Args:
        location: A Location just resolved onto a place.

    Returns:
        Whether the wiki took the parcel.
    """
    from django.db import IntegrityError, transaction

    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.places.lineage import enclosing_parcel

    parcel = enclosing_parcel(location.place) if location.place_id else None
    if parcel is None or Wiki.objects.filter(place=parcel).exists():
        return False
    try:
        with transaction.atomic():
            return bool(Wiki.objects.filter(location=location, place__isnull=True, parent_wiki__isnull=True).update(place=parcel))
    except IntegrityError:
        # Another wiki took the parcel meanwhile.
        return False


def reconcile_wiki_nesting_for_location(location: Location) -> int:
    """Reconcile nesting for a Location's wiki, if it has one.

    Args:
        location: The Location whose boundaries were just (re)generated.

    Returns:
        How many wikis were newly nested; 0 when the location has no wiki."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.get_for_location(location)
    return reconcile_wiki_nesting(wiki) if wiki is not None else 0
