"""REData-backed archival/reference-document clients: the Media gallery's four name-searched archives (Smithsonian Open Access, Library of Congress, Internet Archive, Digital Commonwealth) and the two geosearchable providers (Wikipedia, Wikidata) behind a pin-detail info panel."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway
from urbanlens.dashboard.services.geo.geo_boundary import USA, state_boundary

if TYPE_CHECKING:
    from collections.abc import Generator

    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary

_SEARCH_PATH = "/api/v1/reference-documents/search/"
_NEAR_PATH = "/api/v1/reference-documents/"


@dataclass(slots=True, kw_only=True)
class RedataReferenceDocumentsGateway(RedataLocationContextGateway):
    """REST client for REData's two ``/api/v1/reference-documents/`` endpoints. :meth:`search` (``.../search/``) is not a near-a-coordinate lookup - ``lat``/``lng`` are optional region hints, there's no ``radius_meters``, and the required parameter is..."""

    service_key: ClassVar[str] = "redata_reference_documents"

    def search(
        self,
        query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int | None = None,
        provider: str | list[str] | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Search archival/reference material by name.

        Returns:
            The envelope's ``results`` list - dicts carrying at least ``provider``, ``title``, ``url``, ``thumbnail_url``, ``date_text`` and ``license`` (REData's own field names - see the "reference documents" section of ``api-reference.md``)."""
        params: dict[str, Any] = {"q": query}
        if latitude is not None and longitude is not None:
            params["lat"] = latitude
            params["lng"] = longitude
        if limit is not None:
            params["limit"] = limit
        if provider is not None:
            params["provider"] = provider
        if force_refresh:
            params["force_refresh"] = "true"
        envelope = self._get_envelope(_SEARCH_PATH, params)
        return envelope.results

    def get_reference_documents(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_meters: float | None = None,
        provider: str | list[str] | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Archival/encyclopaedic material about a coordinate.
        Two providers, both with a real geosearch index (unlike :meth:`search`'s four name-only archives) - see ``../REData/docs/api-reference.md``, "GET /reference-documents/ - archival material about a coordinate":

        Returns:
            The parsed envelope.

        Raises:
            LocationContextUnavailableError: A total blackout (every source covering the coordinate failed), a REData-side validation error, or the request itself failed outright."""
        return self.near_point(_NEAR_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider, force_refresh=force_refresh)


@dataclass(slots=True, kw_only=True)
class _RedataReferenceDocumentProvider(MediaProvider):
    """Base for one REData ``reference-documents/search`` archive in the Media gallery."""

    _redata_provider: ClassVar[str] = ""

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        """Yield this archive's matches for ``search_term``, via REData."""
        if not search_term:
            return
        gateway = RedataReferenceDocumentsGateway()
        for doc in gateway.search(search_term, provider=self._redata_provider):
            url = doc.get("url") or ""
            if not url:
                continue
            yield MediaItem(
                url=url,
                thumb_url=doc.get("thumbnail_url") or "",
                caption=doc.get("title") or "",
                source=self.display_name,
                page_url=url,
            )


@dataclass(slots=True, kw_only=True)
class SmithsonianMediaProvider(_RedataReferenceDocumentProvider):
    """Smithsonian Open Access archival media, via REData."""

    service_key: ClassVar[str] = "smithsonian"
    display_name: ClassVar[str] = "Smithsonian Open Access"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "smithsonian"

    # Smithsonian metadata essentially never carries a literal street address anyway.
    include_address: ClassVar[bool] = False
    # A bare, unquoted "United States" is one of the most common phrases in a
    # US federal collection and would otherwise contribute noise as an
    # independent OR term.
    search_with_country: ClassVar[bool] = False
    reject_address_derived_names: ClassVar[bool] = True


@dataclass(slots=True, kw_only=True)
class LibraryOfCongressMediaProvider(_RedataReferenceDocumentProvider):
    """Library of Congress archival media, via REData."""

    service_key: ClassVar[str] = "library_of_congress"
    display_name: ClassVar[str] = "Library of Congress"
    paid_service: ClassVar[bool] = False
    geo_boundary: ClassVar[GeoBoundary | None] = USA
    _redata_provider: ClassVar[str] = "library_of_congress"

    search_with_country: ClassVar[bool] = False
    # LOC's query parser mishandles punctuation inside a quoted phrase and treats each bare word as
    # an independent OR term - a house number or generic street-type word ("Road", "Street")
    # coincidentally matches unrelated historical records nationwide instead of narrowing results.
    # Searching on name + city/state only is both more selective and a better fit for how LOC's
    include_address: ClassVar[bool] = False
    # A pin with no real landmark name (just its raw street address as a fallback "name") produces a
    # search with no genuine narrowing power for LOC's word-independent relevance ranking - skip the
    # provider entirely for such a pin instead of guaranteeing noisy results.
    reject_address_derived_names: ClassVar[bool] = True


@dataclass(slots=True, kw_only=True)
class DigitalCommonwealthMediaProvider(_RedataReferenceDocumentProvider):
    """Digital Commonwealth (Massachusetts statewide archives), via REData.
    Unlike its three siblings, this provider never had a direct UrbanLens gateway in production - a raw ``DigitalCommonwealthGateway`` existed but nothing ever called it, so Massachusetts pins simply lacked the archive."""

    service_key: ClassVar[str] = "digital_commonwealth"
    display_name: ClassVar[str] = "Digital Commonwealth"
    paid_service: ClassVar[bool] = False
    #: A Massachusetts-only collection: querying it for a pin in Ohio can only
    #: return coincidental noise, same reasoning as LOC's USA gate.
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("MA")
    _redata_provider: ClassVar[str] = "digital_commonwealth"

    # Same word-independent relevance ranking as the other archives (it's a
    # Blacklight/Solr catalog): a street address or a bare state name becomes
    # OR-term noise rather than a narrowing signal.
    include_address: ClassVar[bool] = False
    search_with_country: ClassVar[bool] = False
    reject_address_derived_names: ClassVar[bool] = True


@dataclass(slots=True, kw_only=True)
class InternetArchiveMediaProvider(_RedataReferenceDocumentProvider):
    """Internet Archive archival media, via REData."""

    service_key: ClassVar[str] = "internet_archive"
    display_name: ClassVar[str] = "Internet Archive"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "internet_archive"

    # advancedsearch.php's relevance ranking treats space-separated words as independent OR terms
    # rather than requiring a phrase match, so a street address - especially a generic street-type
    # word or a bare house number - coincidentally matches unrelated items nationwide.
    # Searching on name + city/state only is both more selective and a better fit for how Internet
    include_address: ClassVar[bool] = False
    # A bare "United States" adds nothing to a query already anchored on a
    # city/state phrase, and matches an enormous share of a US-heavy archive.
    search_with_country: ClassVar[bool] = False
    # A pin whose only "name" is its raw street address gives the name clause
    # no real narrowing power, so skip the provider rather than guarantee noise.
    reject_address_derived_names: ClassVar[bool] = True


@dataclass(slots=True, kw_only=True)
class ChroniclingAmericaMediaProvider(_RedataReferenceDocumentProvider):
    """Chronicling America historic newspapers (1794-1963), via REData.
    The Library of Congress's digitised-newspaper corpus, published separately from the general ``library_of_congress`` search so dated press coverage can be requested without photographs and maps drowning it out."""

    service_key: ClassVar[str] = "chronicling_america"
    display_name: ClassVar[str] = "Historic Newspapers"
    paid_service: ClassVar[bool] = False
    #: The corpus is US newspapers only, same reasoning as LOC's USA gate.
    geo_boundary: ClassVar[GeoBoundary | None] = USA
    _redata_provider: ClassVar[str] = "chronicling_america"

    # Same LOC search infrastructure as library_of_congress (word-independent
    # relevance ranking, punctuation-hostile quoting), and 1794-1963 newspaper
    # text never contains a modern street address anyway.
    include_address: ClassVar[bool] = False
    search_with_country: ClassVar[bool] = False
    reject_address_derived_names: ClassVar[bool] = True
