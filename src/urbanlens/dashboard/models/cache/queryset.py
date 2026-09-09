"""GeocodedLocation queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.cache.model import GeocodedLocation  # noqa: F401 - mypy resolves these in the class-base subscripts below; ruff does not


class GeocodedLocationQuerySet(abstract.DashboardQuerySet["GeocodedLocation"]):
    """QuerySet for cached geocoding API responses."""


class GeocodedLocationManager(abstract.DashboardManager.from_queryset(GeocodedLocationQuerySet)):
    """Manager for GeocodedLocation cache records."""
