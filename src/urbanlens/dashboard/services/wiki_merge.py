"""Automatic parent/child nesting between community Wikis whose boundaries nest.

Two Wikis are independent, user-initiated community pages (`services.locations.
creation.WikiCreationService`) - nothing stops someone creating one for a
building and, later, someone else creating one for the campus that building
sits on. Once both have a real property boundary, that is no longer two
unrelated places: it is a parcel and one of its own buildings, which should
read as parent and child exactly like a pin and its detail pins do. The
ROADMAP is explicit that this merge should happen "without needing user
confirmation" - unlike the pin-restructure suggestion, there is no dialog here.

Merging is nothing more than setting `parent_wiki`: `Wiki.location` is a
OneToOne, so no Pin, Article, comment, or edit history ever needs to move -
everything already resolves through the unchanged Location, and the merged
Wiki keeps every one of its own child wikis (multi-level nesting is exactly
what `Wiki.parent_wiki` already supports).

Reconciliation runs after a Location's boundaries are (re)generated (see
`tasks.enrich_wiki_location` / `tasks.generate_boundaries_for_location`) -
the only two places a Wiki's property polygon can newly exist or change. It
is idempotent: an already-nested pair is simply not re-matched, so calling it
repeatedly (once per boundary regeneration, forever) is safe.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.db.models.functions import Area

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


def wiki_property_polygon(wiki: Wiki):
    """The wiki's real property boundary, or None when only the fallback circle exists.

    A synthesized circle must never drive an automatic, no-confirmation merge -
    every wiki within 50 m of a bigger one would otherwise silently become its
    child. Mirrors ``services.pin_restructure.property_polygon``.

    Args:
        wiki: The wiki whose property boundary to resolve.

    Returns:
        A real (community-drawn or provider-generated) property polygon, or None.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    polygon, source = Boundary.objects.resolve_for_wiki(wiki, BoundaryType.PROPERTY)
    return polygon if source != "circle" else None


def _root_wikis_inside(polygon, *, exclude_pk: int):
    """Other root wikis whose location falls inside a polygon, smallest boundary first.

    Args:
        polygon: The candidate parent's property polygon.
        exclude_pk: PK to never match against itself.

    Returns:
        A list of candidate child :class:`Wiki` rows.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    return list(
        Wiki.objects.filter(parent_wiki__isnull=True, location__point__within=polygon).exclude(pk=exclude_pk).select_related("location"),
    )


def _containing_root_wiki(wiki: Wiki) -> Wiki | None:
    """The tightest-fitting root wiki whose property boundary contains this wiki's location.

    Several candidates can contain the same point (a building inside a wing
    inside a campus); the smallest by real-world area is the immediate parent,
    not the outermost ancestor - the outer ones will absorb it in turn on
    their own next reconciliation.

    Args:
        wiki: The wiki looking for a bigger container.

    Returns:
        The best-fit containing root wiki, or None.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType

    candidate = (
        Boundary.objects.location_defaults()
        .filter(boundary_type=BoundaryType.PROPERTY, generated_polygon__contains=wiki.location.point)
        .exclude(location_id=wiki.location_id)
        .filter(location__wiki__isnull=False, location__wiki__parent_wiki__isnull=True)
        .annotate(area=Area("generated_polygon"))
        .order_by("area")
        .select_related("location__wiki")
        .first()
    )
    return candidate.location.wiki if candidate is not None else None


def _absorb(parent: Wiki, child: Wiki) -> None:
    """Nest ``child`` under ``parent`` and log it on the parent's edit history.

    Also mutates ``child.parent_wiki`` on the in-memory object, not just the
    database row: within one ``reconcile_wiki_nesting`` call, direction 1 can
    absorb ``wiki`` before direction 2 asks whether some *other* candidate
    would create a cycle *through* wiki - a check that walks ``parent_wiki``
    on this exact Python object. A DB-only ``update()`` would leave that walk
    reading the pre-absorb (stale) chain and miss a cycle direction 1 just
    created, absorbing two wikis into each other.

    Args:
        parent: The wiki that gains a child.
        child: The wiki being nested.
    """
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

    Two independent checks, run in order:

    1. Does a bigger root wiki's property boundary now contain this wiki's
       location? If so, this wiki becomes its child - impossible to create a
       cycle here, since the candidate parent is always root.
    2. Do any *other* root wikis' locations now fall inside this wiki's own
       property boundary? If so, they become this wiki's children (guarded
       against a cycle regardless, in case of degenerate identical-geometry
       boundaries).

    Args:
        wiki: The wiki whose boundary was just (re)generated.

    Returns:
        How many wikis were newly nested (0, 1, or 2 - this wiki plus however
        many it absorbed).
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    # Re-fetched rather than trusted from the caller: direction 1's guard
    # below reads parent_wiki_id, and an in-memory object holding a
    # since-superseded value (e.g. set moments earlier by a *different*
    # wiki's own reconciliation absorbing this one) would otherwise re-run
    # that search and silently overwrite a just-established, tighter parent
    # with an outer one. Every real call site already passes a freshly loaded
    # wiki, so this is a no-op extra read for them and a guard for the rest.
    wiki = Wiki.objects.select_related("location").get(pk=wiki.pk)
    merged = 0

    if wiki.parent_wiki_id is None:
        parent = _containing_root_wiki(wiki)
        if parent is not None and not wiki.would_create_cycle(parent):
            _absorb(parent, wiki)
            merged += 1

    polygon = wiki_property_polygon(wiki)
    if polygon is not None:
        for candidate in _root_wikis_inside(polygon, exclude_pk=wiki.pk):
            if candidate.would_create_cycle(wiki):
                continue
            _absorb(wiki, candidate)
            merged += 1

    return merged


def reconcile_wiki_nesting_for_location(location: Location) -> int:
    """Reconcile nesting for a Location's wiki, if it has one.

    The thin entry point used right after boundary generation, where callers
    have a Location (not necessarily a Wiki) in hand.

    Args:
        location: The Location whose boundaries were just (re)generated.

    Returns:
        How many wikis were newly nested; 0 when the location has no wiki.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    wiki = Wiki.objects.get_for_location(location)
    return reconcile_wiki_nesting(wiki) if wiki is not None else 0
