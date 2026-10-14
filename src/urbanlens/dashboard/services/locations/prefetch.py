"""What is gathered for a newly created Location before anyone opens it."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


class LocationPrefetcher(Protocol):
    """A service that caches what it knows about a Location, so the first page that wants it finds it stored."""

    def prefetch(self, location: Location, latitude: float, longitude: float) -> None:
        """
        Cache the service's answer for ``location`` unless it is already stored.

        Args:
            location: The new Location.
            latitude: Its latitude.
            longitude: Its longitude.
        """
        ...


def run_prefetchers(location: Location, latitude: float, longitude: float) -> None:
    """
    Run every contributed prefetcher for ``location``; one failing never stops the others.

    Args:
        location: The new Location.
        latitude: Its latitude.
        longitude: Its longitude.
    """
    from urbanlens.dashboard.plugins.points import LOCATION_PREFETCHERS

    for prefetcher in LOCATION_PREFETCHERS:
        try:
            prefetcher.prefetch(location, latitude, longitude)
        except Exception:
            logger.exception("run_prefetchers: %s failed for location %s", type(prefetcher).__name__, location.pk)
