"""Resolve which wikis a scanned device's location falls inside."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.wiki.model import Wiki


def wikis_containing_point(point: Point) -> list[Wiki]:
    """Every wiki (including child wikis) whose official boundary contains *point*.

    Reuses the exact location-default-boundary containment query already
    proven in ``services.wiki_access.wikis_hidden_by_pin_move``: only
    ``generated_polygon`` on a boundary with no owning pin/wiki/profile and no
    per-provider ``source`` is ever consulted, never a user-editable one, so
    this can't be gamed by inflating a boundary. Child wikis are included for
    free - each has its own ``Location``/boundary, just like a top-level one.

    Args:
        point: The device's estimated location (SRID 4326).

    Returns:
        Matching wikis, each with its ``location`` pre-selected. Empty when
        the point falls inside no wiki's boundary at all.
    """
    from urbanlens.dashboard.models.boundary.model import Boundary
    from urbanlens.dashboard.models.wiki.model import Wiki

    location_ids = Boundary.objects.filter(
        pin__isnull=True,
        wiki__isnull=True,
        profile__isnull=True,
        source="",
        location__wiki__isnull=False,
        generated_polygon__contains=point,
    ).values_list("location_id", flat=True)
    return list(Wiki.objects.filter(location_id__in=location_ids).select_related("location"))
