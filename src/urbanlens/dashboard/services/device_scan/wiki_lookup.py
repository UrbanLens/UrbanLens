"""Resolve which wikis a scanned device's location falls inside."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.wiki.model import Wiki


def wikis_containing_point(point: Point) -> list[Wiki]:
    """Every wiki (including child wikis) whose official geometry contains *point*.
    Reads ``Place.geometry``, which only the provider chain and boundary voting ever write - never a user- or community-drawn shape, so a device sighting can't be attributed to somebody's inflated drawing.

    Args:
        point: The device's estimated location (SRID 4326).

    Returns:
        Matching wikis, each with its ``location`` pre-selected."""
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.models.wiki.model import Wiki

    place_ids = Place.objects.current().filter(geometry__isnull=False, geometry__contains=point, wiki__isnull=False).values_list("pk", flat=True)
    return list(Wiki.objects.filter(place_id__in=place_ids).select_related("location", "place"))
