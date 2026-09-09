"""REData-backed archival/reference-document clients: the Media gallery's four
name-searched archives (Smithsonian Open Access, Library of Congress, Internet
Archive, Digital Commonwealth) and the two geosearchable providers
(Wikipedia, Wikidata) behind a pin-detail info panel.

REData's ``GET /api/v1/reference-documents/search/`` (``../REData/docs/api-reference.md``,
"GET /reference-documents/search/ - archival material by name") fronts the
four name-only archives - ``internet_archive`` (worldwide),
``library_of_congress`` (USA, keyless) and ``smithsonian`` (USA, needs
``RD_SMITHSONIAN_API_KEY`` - on REData's side now, not this project's) and
``digital_commonwealth`` (Massachusetts, keyless).

These four archives have no coordinate index at all - REData only ever
searches them by name, with ``lat``/``lng`` accepted purely as a *region hint*
("is this regional collection worth asking"), never as a coordinate query.
UrbanLens's own gateways used to build a per-archive-tuned query string
client-side (exact-phrase quoting for Smithsonian's Solr-family parser,
deliberately *not* quoting for LOC's, a hand-rolled field-scoped boolean query
for Internet Archive to work around ``advancedsearch.php``'s full-text
rewrite). REData now builds each archive's actual upstream query server-side,
informed by its own ``query_styles`` response block (``quote_phrases``,
``include_address``, ``include_country`` per provider - see the doc section
above) - so this module passes a clean, unquoted name (optionally with a
locality) as ``q`` and lets REData apply the quoting/phrasing each archive's
parser actually needs, rather than double-applying UrbanLens's own client-side
tuning on top of REData's.

What's still this project's call (REData has no way to un-mix it out of an
opaque ``q`` string): whether a raw street address or country name belongs in
that text at all. ``include_address``/``search_with_country``/
``reject_address_derived_names`` are preserved per archive from the old
gateways for that reason - each was learned from a live failure where a
generic street-type word or a bare "United States" coincidentally matched
unrelated nationwide records under that archive's word-independent relevance
ranking, a problem REData's own quoting can't fix since it never sees the
address/name split.

``GET /api/v1/reference-documents/`` (``api-reference.md``, "GET /reference-documents/ -
archival material about a coordinate") is the other half: Wikipedia and Wikidata both
maintain a real geosearch index, so unlike the four archives above they answer a coordinate
directly, no name needed, through the shared near-a-coordinate contract
(:meth:`~RedataReferenceDocumentsGateway.get_reference_documents`). Wikipedia carries prose
(an intro extract); Wikidata carries structured claims (what a thing *is*, when it was built,
who designed it, its heritage status) - see that method's own docstring for the field mapping.
"""

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
    """REST client for REData's two ``/api/v1/reference-documents/`` endpoints.

    :meth:`search` (``.../search/``) is not a near-a-coordinate lookup - ``lat``/``lng``
    are optional region hints, there's no ``radius_meters``, and the required parameter is
    ``q`` - so it builds its own params dict and calls :meth:`_get_envelope` directly.
    :meth:`get_reference_documents` (the bare path) fits the shared near-a-coordinate
    contract exactly and is a thin wrapper over
    :meth:`~RedataLocationContextGateway.near_point`. Both share this class's
    ``service_key`` (and so its rate-limit budget) since they're the same REData endpoint
    family with the same outbound cost profile.
    """

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

        Args:
            query: Free-text search string - a clean name (optionally with a
                locality), never pre-quoted; REData applies each provider's own
                quoting per its ``query_styles``.
            latitude: WGS-84 latitude - a region hint only, never sent as a
                coordinate search (see the module docstring).
            longitude: WGS-84 longitude - see ``latitude``.
            limit: Bounded positive integer.
            provider: Restrict to one or more provider tags (e.g.
                ``"smithsonian"``) - repeatable, matching REData's ``?provider=``.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The envelope's ``results`` list - dicts carrying at least
            ``provider``, ``title``, ``url``, ``thumbnail_url``, ``date_text``
            and ``license`` (REData's own field names - see the "reference
            documents" section of ``api-reference.md``). Empty when nothing
            matched.
        """
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

        Two providers, both with a real geosearch index (unlike :meth:`search`'s four
        name-only archives) - see ``../REData/docs/api-reference.md``, "GET
        /reference-documents/ - archival material about a coordinate":

        - ``wikipedia`` - articles, each with an intro extract (prose someone chose to
          write).
        - ``wikidata`` - structured claims about the same kind of entities: what a thing
          *is* (``attributes.instance_of``), when it was built (``date_text``), who
          designed it (``creator``), its architectural style
          (``attributes.architectural_style``) and heritage designation
          (``attributes.heritage_designation``). Frequently the more useful half for
          property research, per REData's own docs.

        Wikidata's public query service throttles hard during its own incidents - that
        comes back as REData's own ``rate_limited`` (503), surfaced the same way any other
        provider outage in this shared contract is: a ``providers`` entry plus
        ``complete=False``, never a silent empty result. Its claim enrichment is a second,
        separate query that can degrade on its own: a Wikidata row can come back
        labelled (``title``/``description``/``url``) but with blank ``date_text``/
        ``creator`` and no matching ``attributes`` keys - a normal 200 from REData's point
        of view, not something this method detects or needs to. Callers render whatever
        claim fields are present and skip the rest, rather than treating a partial claim
        set as a failure.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_meters: Search radius in meters. REData defaults to 1 km and caps at
                10 km.
            provider: Restrict to ``"wikipedia"``, ``"wikidata"``, or both (a list) -
                omit to ask both.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. Each ``results`` entry is one of REData's
            ``ReferenceDocumentSerializer`` rows: ``provider``, ``kind``, ``title``,
            ``description`` (Wikipedia's intro extract; a short gloss for Wikidata),
            ``url``, ``thumbnail_url``, ``date_text``, ``creator``, ``license``,
            ``latitude``/``longitude``, ``distance_meters`` and ``attributes``
            (provider-specific - a Wikidata row's carries its raw claims).

        Raises:
            LocationContextUnavailableError: A total blackout (every source covering the
                coordinate failed), a REData-side validation error, or the request itself
                failed outright.
        """
        return self.near_point(_NEAR_PATH, latitude, longitude, radius_meters=radius_meters, provider=provider, force_refresh=force_refresh)


@dataclass(slots=True, kw_only=True)
class _RedataReferenceDocumentProvider(MediaProvider):
    """Base for one REData ``reference-documents/search`` archive in the Media gallery.

    Subclasses set ``_redata_provider`` (REData's own ``?provider=`` tag);
    ``service_key``/``display_name`` stay each archive's own historical value
    so the ``LocationCache`` rows, cache keys and gallery tab label written
    under the old direct gateways keep working unchanged.
    """

    _redata_provider: ClassVar[str] = ""

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        """Yield this archive's matches for ``search_term``, via REData.

        Args:
            search_term: A clean, unquoted query built by
                ``MediaPanelSource.search_terms`` per this class's
                ``include_address``/``search_with_country``/``quote_*`` flags.
            address: Unused - REData has no separate address parameter; any
                address this provider wants is already folded into
                ``search_term`` (see ``include_address``).
        """
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

    # A raw, unquoted street address (house number + generic street-type word)
    # is treated as independent OR terms by Smithsonian's Solr-family query
    # parser and coincidentally matches unrelated records across the ~19M-object
    # collection - see the module docstring. Smithsonian metadata essentially
    # never carries a literal street address anyway.
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
    # LOC's query parser mishandles punctuation inside a quoted phrase and
    # treats each bare word as an independent OR term - a house number or
    # generic street-type word ("Road", "Street") coincidentally matches
    # unrelated historical records nationwide instead of narrowing results.
    # Searching on name + city/state only is both more selective and a better
    # fit for how LOC's collections are catalogued (historical documents/photos
    # rarely carry modern street-address-level metadata anyway).
    include_address: ClassVar[bool] = False
    # A pin with no real landmark name (just its raw street address as a
    # fallback "name") produces a search with no genuine narrowing power for
    # LOC's word-independent relevance ranking - skip the provider entirely for
    # such a pin instead of guaranteeing noisy results.
    reject_address_derived_names: ClassVar[bool] = True


@dataclass(slots=True, kw_only=True)
class DigitalCommonwealthMediaProvider(_RedataReferenceDocumentProvider):
    """Digital Commonwealth (Massachusetts statewide archives), via REData.

    Unlike its three siblings, this provider never had a direct UrbanLens
    gateway in production - a raw ``DigitalCommonwealthGateway`` existed but
    nothing ever called it, so Massachusetts pins simply lacked the archive.
    REData has fronted the provider all along; this class is the missing
    UrbanLens half.
    """

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
    """Internet Archive archival media, via REData.

    The old ``InternetArchiveGateway`` hand-built a field-scoped Lucene boolean
    query and re-checked relevance locally to work around
    ``advancedsearch.php``'s bare-keyword full-text rewrite (see that deleted
    module's docstring) - that entire workaround is now REData's problem to
    solve server-side, so this provider is a plain name + locality search.
    """

    service_key: ClassVar[str] = "internet_archive"
    display_name: ClassVar[str] = "Internet Archive"
    paid_service: ClassVar[bool] = False
    _redata_provider: ClassVar[str] = "internet_archive"

    # advancedsearch.php's relevance ranking treats space-separated words as
    # independent OR terms rather than requiring a phrase match, so a street
    # address - especially a generic street-type word or a bare house number -
    # coincidentally matches unrelated items nationwide. Searching on name +
    # city/state only is both more selective and a better fit for how Internet
    # Archive's collections are catalogued.
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

    The Library of Congress's digitised-newspaper corpus, published separately
    from the general ``library_of_congress`` search so dated press coverage
    can be requested without photographs and maps drowning it out. Results
    are newspaper *pages* with the paper's own title and city - for a site's
    back-story ("MILL DESTROYED BY FIRE", the sale notice, the strike
    coverage), the local paper is routinely the only surviving record.
    """

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
