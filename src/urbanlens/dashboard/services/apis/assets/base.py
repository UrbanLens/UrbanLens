"""Shared abstraction for gateways that return captioned media (photos, scans, etc.)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
import logging
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

from urbanlens.dashboard.services.core.gateway import Gateway, is_source_outage
from urbanlens.dashboard.services.core.tracking_params import without_tracking_params

if TYPE_CHECKING:
    from collections.abc import Generator

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary

logger = logging.getLogger(__name__)

#: Content types of paged documents.
PAGED_DOCUMENT_CONTENT_TYPES = frozenset({"application/pdf", "image/vnd.djvu", "image/x-djvu"})
PAGED_DOCUMENT_EXTENSIONS = (".pdf", ".djvu", ".djv")

#: Mirrors ``LocationCache.query_key``'s ``max_length``. Imported lazily
#: everywhere else in this module to avoid a model import at module scope.
_QUERY_KEY_MAX_LENGTH = 255


@dataclass(frozen=True)
class MediaItem:
    """A single piece of captioned media from an external archive.

    Its URLs are kept without tracking parameters, so a file has one identity however the provider links it.

    Attributes:
        url: Full-resolution image URL.
        thumb_url: Thumbnail URL, or ``""`` when the provider has no preview image for this item (e.g. a text/document record) - the frontend renders a fallback icon tile in that case instead of dropping it.
        caption: Human-readable caption or title.
        source: Human-readable provider name (e.g. ``"Smithsonian Open Access"``).
        page_url: Link to the item's page on the provider's site, if any.
        content_type: The provider-declared content type of ``url``, when it publishes one.
        author: Who to credit for the photo itself, distinct from ``source`` (the provider/archive).
        title: The item's own title, when the provider publishes one apart from ``caption``.
        description: The provider's description of the item.
        keywords: Subjects, categories or tags the provider files the item under, joined with ``|``.
        latitude: Where the provider places the item, when it does.
        longitude: See ``latitude``."""

    url: str
    thumb_url: str
    caption: str
    source: str
    page_url: str = ""
    content_type: str = ""
    author: str = ""
    title: str = ""
    description: str = ""
    keywords: str = ""
    latitude: float | None = None
    longitude: float | None = None

    def __post_init__(self) -> None:
        for name in ("url", "thumb_url", "page_url"):
            object.__setattr__(self, name, without_tracking_params(getattr(self, name)))

    @property
    def is_document(self) -> bool:
        """Whether this is a paged document (a PDF or DjVu book or scan), which belongs on Article > Sources rather than in a gallery."""
        content_type = self.content_type.lower()
        if "pdf" in content_type or "djvu" in content_type:
            return True
        return urlsplit(self.url).path.lower().endswith(PAGED_DOCUMENT_EXTENSIONS)


class MediaProvider(Gateway, ABC):
    """Template for gateways that return captioned media for a Location.
    Subclasses implement ``_generate_media`` to yield ``MediaItem``s for a search term; ``get_media`` wraps that with the shared 7-day ``LocationCache``, so results are only fetched once per Location."""

    display_name: ClassVar[str] = "Media"
    #: Restricts this provider to a geographic region (see ``services.geo.geo_boundary``);
    #: None means unrestricted. Enforced by ``MediaPanelSource.gate``.
    geo_boundary: ClassVar[GeoBoundary | None] = None
    search_with_country: ClassVar[bool] = True
    quote_name: ClassVar[bool] = False
    # Whether "city state" is wrapped as one quoted phrase instead of two loose keywords.
    # Same rationale as ``quote_name``: a provider whose relevance ranking treats query words as
    # independent OR terms will otherwise let a bare city or state name (e.g.
    quote_locality: ClassVar[bool] = False
    multi_query: ClassVar[bool] = False
    # Whether the street address is included in the search query at all.
    # A provider whose full-text relevance ranking treats every word as an independent OR term
    # (rather than requiring a phrase match) can turn a street address into noise instead of a
    # useful narrowing signal - a street number or a generic street-type word ("Road", "Street") is
    include_address: ClassVar[bool] = True
    # Whether to skip this provider entirely (no search attempted) for a pin whose only available
    # "name" is address-derived (see services.locations.naming.is_address_derived_name) - a query
    # built from a raw street address has no real narrowing power for a provider whose relevance
    # ranking isn't a phrase match, so searching guarantees noise rather than useful results for
    reject_address_derived_names: ClassVar[bool] = False

    def available(self) -> bool:
        """Whether this install can ask this provider at all. An unavailable one is not scheduled and caches nothing,
        so its first answer once configured is a real one.

        Returns:
            True unless the provider needs configuration this install lacks.
        """
        return True

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

    def get_media(
        self,
        location: Location,
        search_terms: list[str],
        *,
        address: str | None = None,
        limit: int = 24,
        audience: str = "",
        search_names: dict[str, list[str]] | None = None,
    ) -> tuple[list[MediaItem], bool]:
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
            limit: Maximum number of gallery items to return. Documents do not count: they are listed under Article >
                Sources, which a gallery's size must not decide.
            audience: Whose ``LocationCache`` row the results go in; the default is the one every viewer shares.
            search_names: The names the terms were built from, kept on the row.

        Returns:
            Tuple of (list of ``MediaItem``s, empty when the provider found nothing; whether the result was served from cache).

        Raises:
            Exception: The outage that left every query unanswered, so that nothing is cached (see ``is_source_outage``).
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if (service_key := self.service_key) is None:
            raise RuntimeError(f"{type(self).__name__} has no service_key configured")
        if not self.available():
            return [], False

        # Truncated to LocationCache.query_key's own max_length so the value compared below is the
        # value that can actually be stored - otherwise an over-long key would never match what came
        # back and every request would re-fetch, hammering the provider instead of caching.
        query_key = " | ".join(term for term in search_terms if term)[:_QUERY_KEY_MAX_LENGTH]
        cached = LocationCache.get_fresh(location, service_key, audience)
        # A cache row is only a hit for the query that produced it.
        # LocationCache.get_fresh answers "is this row still fresh?" by age alone, so without this
        # check a provider whose query construction has been changed (e.g. tightened for relevance)
        # keeps serving results fetched by the *old* query for the rest of the 7-day TTL - the fix
        if cached is not None and (cached.query_key or "") == query_key:
            return [MediaItem(**item) for item in (cached.data or {}).get("items", [])], True

        items: list[MediaItem] = []
        seen_urls: set[str] = set()
        gallery_items = 0
        outage: Exception | None = None
        for search_term in search_terms:
            if not search_term or (limit > 0 and gallery_items >= limit):
                continue
            try:
                for item in self._generate_media(search_term, address):
                    if item.url in seen_urls or (not item.is_document and limit > 0 and gallery_items >= limit):
                        continue
                    seen_urls.add(item.url)
                    items.append(item)
                    if not item.is_document:
                        gallery_items += 1
            except Exception as exc:
                # TODO: Catch specific exceptions
                if is_source_outage(exc):
                    logger.warning("%s media lookup unavailable for %r: %s", self.service_key, search_term, exc)
                    outage = exc
                else:
                    logger.exception("%s media lookup failed for %r", self.service_key, search_term)

        if outage is not None and not items:
            raise outage

        data: dict = {"items": [asdict(item) for item in items]}
        if search_names is not None:
            data["search_names"] = search_names
        LocationCache.set(location, service_key, data, query_key=query_key, audience=audience)
        return items, False
