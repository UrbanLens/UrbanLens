"""GeocodedLocation queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.cache.model import GeocodedLocation


class GeocodedLocationQuerySet(abstract.QuerySet):
    """QuerySet for cached geocoding API responses."""


class GeocodedLocationManager(abstract.Manager.from_queryset(GeocodedLocationQuerySet)):
    """Manager for GeocodedLocation cache records."""
