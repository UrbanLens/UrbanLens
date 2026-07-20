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
    from urbanlens.dashboard.services.geo_boundary import GeoBoundary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaItem:
    """A single piece of captioned media from an external archive.

    Attributes:
        url: Full-resolution image URL.
        thumb_url: Thumbnail URL, or ``""`` when the provider has no preview
            image for this item (e.g. a text/document record) - the frontend
            renders a fallback icon tile in that case instead of dropping it.
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
    #: Restricts this provider to a geographic region (see ``services.geo_boundary``);
    #: None means unrestricted. Enforced by ``MediaPanelSource.gate``.
    geo_boundary: ClassVar[GeoBoundary | None] = None
    search_with_country: ClassVar[bool] = True
    quote_name: ClassVar[bool] = False
    multi_query: ClassVar[bool] = False
    # Whether the street address is included in the search query at all. A
    # provider whose full-text relevance ranking treats every word as an
    # independent OR term (rather than requiring a phrase match) can turn a
    # street address into noise instead of a useful narrowing signal - a
    # street number or a generic street-type word ("Road", "Street") is
    # liable to coincidentally match unrelated records nationwide. Such a
    # provider should set this False to search on name + city/state only.
    include_address: ClassVar[bool] = True
    # Whether to skip this provider entirely (no search attempted) for a pin
    # whose only available "name" is address-derived (see
    # services.locations.naming.is_address_derived_name) - a query built from
    # a raw street address has no real narrowing power for a provider whose
    # relevance ranking isn't a phrase match, so searching guarantees noise
    # rather than useful results for such a pin.
    reject_address_derived_names: ClassVar[bool] = False

    @abstractmethod
    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        """Yield MediaItems for ``search_term``.

        Args:
            search_term: The search term to use to find media.
            address: The address of the location, if any. Some media providers
                may use this, or quote it, differently than others.

        Returns:
            Generator of ``MediaItem``s.
        """
        ...

    def get_media(self, location: Location, search_terms: list[str], *, address: str | None = None, limit: int = 24) -> tuple[list[MediaItem], bool]:
        """Return captioned media for ``location``, using the 7-day LocationCache.

        Args:
            location: The shared Location to cache results against.
            search_terms: Ordered queries passed to ``_generate_media``, most
                specific first. Every term is tried and results are merged
                (deduped by URL) up to ``limit`` -- some search engines return
                nothing for an overly specific query (e.g. a full street
                address) but do match a broader one, so a single provider may
                be given more than one candidate query to widen recall.
            address: The address of the location, if any. Some media providers
                may use this, or quote it, differently than others.
            limit: Maximum number of items to return.

        Returns:
            Tuple of (list of ``MediaItem``s, empty when the provider found
            nothing or failed; whether the result was served from cache).
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if (service_key := self.service_key) is None:
            raise RuntimeError(f"{type(self).__name__} has no service_key configured")

        cached = LocationCache.get_fresh(location, service_key)
        if cached is not None:
            return [MediaItem(**item) for item in (cached.data or {}).get("items", [])], True

        items: list[MediaItem] = []
        seen_urls: set[str] = set()
        for search_term in search_terms:
            if not search_term or (limit > 0 and len(items) >= limit):
                continue
            try:
                for item in self._generate_media(search_term, address):
                    if item.url in seen_urls:
                        continue
                    seen_urls.add(item.url)
                    items.append(item)
                    if limit > 0 and len(items) >= limit:
                        break
            except Exception:
                # TODO: Catch specific exceptions
                logger.exception("%s media lookup failed for %r", self.service_key, search_term)

        LocationCache.set(
            location,
            service_key,
            {"items": [asdict(item) for item in items]},
            query_key=" | ".join(term for term in search_terms if term),
        )
        return items, False
