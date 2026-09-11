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
        A real (community-drawn or provider-generated) property polygon, or None."""
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    polygon, source = Boundary.objects.resolve_for_wiki(wiki, BoundaryType.PROPERTY)
    return polygon if source != "circle" else None


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

    polygon = wiki_property_polygon(wiki)
    if polygon is None:
        return []
    return list(
        Wiki.objects.filter(parent_wiki__isnull=True, place__isnull=True, location__point__within=polygon).exclude(pk=wiki.pk).select_related("location", "place"),
    )


def _containing_root_wiki(wiki: Wiki) -> Wiki | None:
    """The wiki of the nearest ancestor place that has one.
    The hierarchy was already decided when the places were provisioned - a building is ``PART_OF`` its parcel, a parcel is ``MEMBER_OF`` the site it belongs to - so walking one FK chain gives the tightest fit directly, instead of re-deriving it by sorting every containing polygon by area on every reconciliation.

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
    """Lineage-free fallback for wikis on coordinates no provider knows.
    A placeless wiki has no lineage to walk, so containment against other wikis' *place* outlines is the only signal left.

    Args:
        wiki: The placeless wiki looking for a container.

    Returns:
        The smallest containing root wiki, or None."""
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki

    if wiki.location_id is None or wiki.location.point is None:
        return None
    container = Place.objects.resolve_for_point(wiki.location.latitude, wiki.location.longitude)
    if container is None:
        return None
    return Wiki.objects.filter(place=container, parent_wiki__isnull=True).exclude(pk=wiki.pk).select_related("location", "place").first()


def _absorb(parent: Wiki, child: Wiki) -> None:
    """Nest ``child`` under ``parent`` and log it on the parent's edit history.

    Args:
        parent: The wiki that gains a child.
        child: The wiki being nested."""
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.models.wiki_edit import WikiEdit

    Wiki.objects.filter(pk=child.pk).update(parent_wiki=parent)
    child.parent_wiki = parent
    WikiEdit.objects.create(
        wiki=parent,
        editor=None,
        changes={"child_wiki_merged": {"from": None, "to": child.name}},
    )
    logger.info("wiki_merge: nested wiki %s (%r) under %s (%r) by boundary containment", child.pk, child.name, parent.pk, parent.name)


def reconcile_wiki_nesting(wiki: Wiki) -> int:
    """Automatically nest this wiki under a bigger one, and absorb smaller ones into it.

    Args:
        wiki: The wiki whose place was just (re)resolved.

    Returns:
        How many wikis were newly nested (0, 1, or more - this wiki plus
        however many it absorbed)."""
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
            _absorb(parent, wiki)
            merged += 1

    for candidate in _nestable_child_wikis(wiki):
        if candidate.would_create_cycle(wiki):
            continue
        _absorb(wiki, candidate)
        merged += 1

    return merged


def reconcile_wiki_nesting_for_location(location: Location) -> int:
    """Reconcile nesting for a Location's wiki, if it has one.

    Args:
        location: The Location whose boundaries were just (re)generated.

    Returns:
        How many wikis were newly nested; 0 when the location has no wiki."""
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.get_for_location(location)
    return reconcile_wiki_nesting(wiki) if wiki is not None else 0
