"""QuerySet and manager for Route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.contrib.gis.geos import Polygon

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    import datetime

    from urbanlens.dashboard.models.profile.model import Profile


class RouteQuerySet(abstract.QuerySet):
    """QuerySet for Route records."""

    def for_profile(self, profile: Profile) -> Self:
        """Filter to routes owned by a specific profile.

        Args:
            profile: The owning profile.

        Returns:
            Filtered queryset.
        """
        return self.filter(profile=profile)

    def in_date_range(self, start: datetime.date, end: datetime.date) -> Self:
        """Filter to routes that started within an inclusive date range.

        Args:
            start: Earliest allowed start date.
            end: Latest allowed start date.

        Returns:
            Filtered queryset.
        """
        return self.filter(started_at__date__range=(start, end))

    def intersecting_bbox(self, min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> Self:
        """Filter to routes whose path overlaps a lat/lng bounding box.

        Uses the cheaper index-only ``bboverlaps`` lookup rather than a full
        ``intersects`` test, since this is meant for coarse map-viewport scoping.

        Args:
            min_lat: Southern boundary.
            min_lng: Western boundary.
            max_lat: Northern boundary.
            max_lng: Eastern boundary.

        Returns:
            Filtered queryset.
        """
        bbox = Polygon.from_bbox((min_lng, min_lat, max_lng, max_lat))
        bbox.srid = 4326
        return self.filter(path__bboverlaps=bbox)


class RouteManager(abstract.Manager.from_queryset(RouteQuerySet)):
    """Manager for Route."""
