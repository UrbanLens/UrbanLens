"""Shared abstraction for gateways that return captioned media (photos, scans, etc.).

Mirrors the template-method pattern used by ``SatelliteViewProvider``
(``urbanlens.dashboard.services.apis.locations.base``): a subclass implements
one abstract generator, and the base class owns caching and result limiting.
Unlike the satellite/street-view providers -- which cache by lat/lon in
Django's low-level cache -- media providers are scoped to a shared
``Location`` and cache through ``LocationCache`` (7-day TTL), consistent with
the other Location-scoped external-data lookups (Wikipedia, Nominatim, NPS, ...).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import logging
from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.services.gateway import Gateway

if TYPE_CHECKING:
    from collections.abc import Generator

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaItem:
    """A single piece of captioned media from an external archive.

    Attributes:
        url: Full-resolution image URL.
        thumb_url: Thumbnail URL (falls back to ``url`` when the provider has none).
        caption: Human-readable caption or title.
        source: Human-readable provider name (e.g. ``"Smithsonian Open Access"``).
        page_url: Link to the item's page on the provider's site, if any.
    """

    url: str
    thumb_url: str
    caption: str
    source: str
    page_url: str = ""


class MediaProvider(Gateway, ABC):
    """Template for gateways that return captioned media for a Location.

    Subclasses implement ``_generate_media`` to yield ``MediaItem``s for a
    search term; ``get_media`` wraps that with the shared 7-day
    ``LocationCache``, so results are only fetched once per Location.
    """

    display_name: ClassVar[str] = "Media"
    usa_only: ClassVar[bool] = False
    search_with_country: ClassVar[bool] = True

    @abstractmethod
    def _generate_media(self, search_term: str) -> Generator[MediaItem]:
        """Yield MediaItems for ``search_term``. Implementations should not raise."""
        ...

    def get_media(self, location: Location, search_term: str, *, limit: int = 24) -> list[MediaItem]:
        """Return captioned media for ``location``, using the 7-day LocationCache.

        Args:
            location: The shared Location to cache results against.
            search_term: Human-readable query passed to ``_generate_media``.
            limit: Maximum number of items to return.

        Returns:
            List of ``MediaItem``s, empty when the provider found nothing or failed.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        cached = LocationCache.get_fresh(location, self.service_key)
        if cached is not None:
            return [MediaItem(**item) for item in (cached.data or {}).get("items", [])]

        items: list[MediaItem] = []
        try:
            for item in self._generate_media(search_term):
                items.append(item)
                if limit > 0 and len(items) >= limit:
                    break
        except Exception:
            logger.exception("%s media lookup failed for %r", self.service_key, search_term)
            items = []

        LocationCache.set(
            location,
            self.service_key,
            {"items": [asdict(item) for item in items]},
            query_key=search_term,
        )
        return items
