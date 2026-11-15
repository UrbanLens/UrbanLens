"""
The outlines a place has had.

``Place.geometry`` is the current outline and what queries read. Every write to it goes through :func:`set_geometry`, which appends a
:class:`PlaceGeometryVersion` first, so an earlier outline is never lost to a refresh.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from urbanlens.dashboard.models.place.lifecycle import PlaceGeometryVersion
from urbanlens.dashboard.models.place.model import Place

if TYPE_CHECKING:
    from datetime import datetime

    from django.contrib.gis.geos import MultiPolygon


def latest_version(place: Place) -> PlaceGeometryVersion | None:
    """The most recent outline recorded for a place, flagged or not."""
    return PlaceGeometryVersion.objects.filter(place=place).order_by("-recorded_at", "-pk").first()


def outline_source(place: Place) -> str:
    """
    Which provider drew the place's current outline: that of its latest sound version.

    ``Place.provider`` is the namespace of the place's provider key and says nothing about who drew its outline.
    Blank when the source is unknown (the backfill, or a place with no recorded outline) or is the unnamed provider chain.
    """
    latest = PlaceGeometryVersion.objects.filter(place=place, erroneous=False).order_by("-recorded_at", "-pk").values_list("provider", flat=True).first()
    return latest or ""


def record_version(place: Place, polygon: MultiPolygon, *, provider: str = "", recorded_at: datetime | None = None) -> PlaceGeometryVersion | None:
    """
    Append an outline unless it is the one already on record.

    Returns:
        The new version, or None when ``polygon`` equals the latest recorded outline.
    """
    latest = latest_version(place)
    if latest is not None and not latest.erroneous and latest.geometry.wkb == polygon.wkb:
        return None
    return PlaceGeometryVersion.objects.create(place=place, geometry=polygon, provider=provider, recorded_at=recorded_at or timezone.now())


def set_geometry(place: Place, polygon: MultiPolygon, *, provider: str = "", recorded_at: datetime | None = None) -> bool:
    """
    Make ``polygon`` the place's current outline, keeping the previous one as a version.

    ``provider`` is where ``polygon`` came from; ``Place.provider`` is a key namespace and is not used for it.
    The caller refreshes derived values (``resolution.refresh_area``) and re-resolves locations.

    Returns:
        True when the outline changed.
    """
    with transaction.atomic():
        # The row lock keeps two first writes from both recording the previous outline.
        locked = list(Place.objects.select_for_update().filter(pk=place.pk).values_list("geometry", "geometry_generated_at"))
        stored, stored_at = locked[0] if locked else (None, None)
        had_versions = PlaceGeometryVersion.objects.filter(place=place).exists()
        if not had_versions and stored is not None:
            # The outline stored under the lock, not the caller's possibly stale copy. Who drew it is unknown, as in the backfill.
            record_version(place, stored, provider="", recorded_at=stored_at or place.created)
        changed = record_version(place, polygon, provider=provider, recorded_at=recorded_at) is not None
        Place.objects.filter(pk=place.pk).update(geometry=polygon, updated=timezone.now())
    place.geometry = polygon
    return changed


def mark_erroneous(versions: list[PlaceGeometryVersion]) -> int:
    """Flag outlines as wrong. They stay on record; split detection ignores them."""
    return PlaceGeometryVersion.objects.filter(pk__in=[version.pk for version in versions]).update(erroneous=True)


def correct_geometry(place: Place, polygon: MultiPolygon, *, erroneous: list[PlaceGeometryVersion], provider: str = "") -> None:
    """
    Replace outlines that were wrong.

    Args:
        place: The place.
        polygon: The right outline.
        erroneous: Recorded outlines the correction shows to be wrong.
        provider: Where the correction came from.
    """
    with transaction.atomic():
        mark_erroneous(erroneous)
        set_geometry(place, polygon, provider=provider)
