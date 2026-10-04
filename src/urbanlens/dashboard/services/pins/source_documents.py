"""Documents listed under Article > Sources, gathered from every :class:`DocumentPanelSource` cached for a location."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

import filetype

from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_if_small
from urbanlens.dashboard.services.media.proxied_media import INLINE_MEDIA_TYPES, looks_like_pdf
from urbanlens.dashboard.services.pins.external_data import DocumentPanelSource, DocumentUnavailableError, NameSearchSource, SourceDocument, document_panel_sources, get_panel_source, panel_visible_to

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.media.subject_relevance import MediaSubject

logger = logging.getLogger(__name__)

DOCUMENT_CACHE_TTL = 3600
#: Scanned inventory forms run to several megabytes; sized like the gallery proxy's ceiling so both share entries.
DOCUMENT_MAX_CACHED_BYTES = 4 * 1024 * 1024
PDF_CONTENT_TYPE = "application/pdf"


@dataclass(frozen=True, slots=True)
class ListedDocument:
    """One document and the source that listed it.

    Attributes:
        source: The panel source whose cached payload names it.
        document: The document.
    """

    source: DocumentPanelSource
    document: SourceDocument


@dataclass(frozen=True, slots=True)
class SourceListing:
    """What Article > Sources can show right now.

    Attributes:
        documents: The documents, grouped by source in registry order.
        pending: Whether a fetch is in flight that may add more.
    """

    documents: list[ListedDocument]
    pending: bool


def _cached_payloads(location: Location, sources: Sequence[DocumentPanelSource], reader: Pin | None) -> dict[str, dict | None]:
    """Each source's fresh cached payload for ``location``, as :func:`_cached_payload` reads it, in two queries at most.

    Args:
        location: The location whose cache rows are read.
        sources: The document sources.
        reader: The pin whose own names' rows a name-built source reads too, or None for the shared rows alone.

    Returns:
        Source key to its payload, or to None when nothing fresh has landed.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.pins.external_data import cached_entries
    from urbanlens.dashboard.services.pins.search_names import SHARED_AUDIENCE

    payloads: dict[str, dict | None] = {}
    named = [source for source in sources if isinstance(source, NameSearchSource)] if reader is not None else []
    if named and reader is not None:
        payloads.update({key: None if entry is None else entry.data for key, entry in cached_entries(reader, named).items()})
    shared = [source for source in sources if source.key not in payloads]
    if shared and location.pk is not None:
        rows = LocationCache.fresh_rows(location.pk, {source.cache_source for source in shared}, [SHARED_AUDIENCE])
        for source in shared:
            row = rows.get((source.cache_source, SHARED_AUDIENCE))
            payloads[source.key] = None if row is None else (row.data or {})
    return payloads


def _cached_payload(location: Location, source: DocumentPanelSource, reader: Pin | None = None) -> dict | None:
    """A source's fresh cached payload for ``location``, or None when nothing fresh has landed.

    A name-built source's payload is the shared row's, plus the reader pin's own names' row when it has one.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if reader is not None and isinstance(source, NameSearchSource):
        return source.cached_data(reader)
    row = LocationCache.get_fresh(location, source.cache_source)
    return None if row is None else (row.data or {})


def collect_source_documents(
    location: Location,
    *,
    viewer: AbstractBaseUser | AnonymousUser,
    driver: Pin | None,
    site_scope: bool,
    may_fetch: bool,
    subject: MediaSubject | None = None,
    reader: Pin | None = None,
    nested: Sequence[Location] = (),
) -> SourceListing:
    """Gather every visible source's documents for a location, scheduling a fetch for any source not ready yet.

    A source that is not fetched for Sources (``DocumentPanelSource.fetched_for_sources``) lists what is cached and is
    never scheduled.

    Args:
        location: The location whose cache rows are read.
        viewer: The requesting user; feature-gated sources are skipped for a viewer without the feature.
        driver: The pin a fetch runs as (its owner's quota and ``external_apis_enabled``), or None when there is none.
        site_scope: Whether the page describes a parcel/site rather than one building.
        may_fetch: False once the caller's poll budget is spent.
        subject: The place, which documents found by searching must be about; without it they are not listed.
        reader: The pin whose own names' rows are read too (pin pages), or None for the shared rows alone (wiki pages).
        nested: Locations of the markers nested under this one, whose documents are listed too (see :func:`nested_documents`).

    Returns:
        The listing. A source that is fetching contributes nothing yet; one that cannot fetch lists whatever it has.
    """
    from urbanlens.dashboard.services.pins import external_data

    documents: list[ListedDocument] = []
    pending = False
    sources = [source for source in document_panel_sources() if panel_visible_to(viewer, source)]
    payloads = _cached_payloads(location, sources, reader)
    for source in sources:
        data = payloads.get(source.key)
        if data is None or not source.documents_ready(data, site_scope=site_scope):
            if source.fetched_for_sources and may_fetch and driver is not None and source.gate(driver) and external_data.schedule_panel_fetch(source.key, driver):
                pending = True
                continue
            if data is None:
                continue
        documents.extend(ListedDocument(source, document) for document in source.source_documents(data, site_scope=site_scope, subject=subject))
    if nested:
        listed = {(listed.source.key, listed.document.document_id) for listed in documents}
        documents.extend(document for document in nested_documents(nested, sources) if (document.source.key, document.document.document_id) not in listed)
    return SourceListing(documents=documents, pending=pending)


def nested_documents(locations: Sequence[Location], sources: Sequence[DocumentPanelSource]) -> list[ListedDocument]:
    """The documents the shared cache rows of nested markers' locations list, as each one's own page lists them.

    Never fetches: a nested marker's rows are filled by its own page. Documents found by searching are judged against
    the nested place they were found for.

    Args:
        locations: The nested markers' locations.
        sources: The document sources the viewer may see.

    Returns:
        Each document once, in location order, then source order.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.services.pins.search_names import SHARED_AUDIENCE

    by_id = {location.pk: location for location in locations if location.pk is not None}
    if not by_id or not sources:
        return []
    rows = LocationCache.objects.filter(location_id__in=list(by_id), source__in=[source.cache_source for source in sources], audience=SHARED_AUDIENCE, updated__gte=LocationCache.fresh_since())
    payloads = {(row.location_id, row.source): row.data or {} for row in rows}
    return documents_from_payloads([(location, source, payloads[location.pk, source.cache_source]) for location in by_id.values() for source in sources if (location.pk, source.cache_source) in payloads])


def documents_from_payloads(payloads: Sequence[tuple[Location, DocumentPanelSource, dict]]) -> list[ListedDocument]:
    """List the documents in cached payloads at building scope, each once.

    Args:
        payloads: ``(location, source, data)`` per cached row.

    Returns:
        The documents, in payload order.
    """
    from urbanlens.dashboard.services.media.subject_relevance import subject_for_location

    documents: list[ListedDocument] = []
    seen: set[tuple[str, str]] = set()
    subjects: dict[int | None, MediaSubject] = {}
    for location, source, data in payloads:
        subject = None
        if source.documents_judged and source.may_list_documents(data):
            if location.pk not in subjects:
                subjects[location.pk] = subject_for_location(location)
            subject = subjects[location.pk]
        for document in source.source_documents(data, site_scope=False, subject=subject):
            if (source.key, document.document_id) not in seen:
                seen.add((source.key, document.document_id))
                documents.append(ListedDocument(source, document))
    return documents


def warm_site_scope_documents(pin: Pin) -> None:
    """Fetch every document source not yet ready at site scope, for a pin that has just become a site.

    Args:
        pin: The pin now describing a site.
    """
    from urbanlens.dashboard.services.pins import external_data

    for source in document_panel_sources():
        if not source.documents_depend_on_site_scope or not source.fetched_for_sources:
            continue
        data = _cached_payload(pin.location, source)
        if data is not None and source.documents_ready(data, site_scope=True):
            continue
        if source.gate(pin):
            external_data.schedule_panel_fetch(source.key, pin)


def find_listed_document(
    location: Location,
    source_key: str,
    document_id: str,
    *,
    viewer: AbstractBaseUser | AnonymousUser,
    site_scope: bool,
    nested: Sequence[Location] = (),
) -> ListedDocument | None:
    """The document with this id, only when the location's cached payload lists it for this viewer and scope.

    Args:
        location: The location whose cache rows are read.
        source_key: The panel source key from the URL.
        document_id: The document id from the URL.
        viewer: The requesting user.
        site_scope: Whether the page describes a parcel/site rather than one building.
        nested: Locations of the markers nested under this one; a document their rows list (see
            :func:`nested_documents`) is found too.

    Returns:
        The listed document, or None.
    """
    source = get_panel_source(source_key)
    if not isinstance(source, DocumentPanelSource) or not panel_visible_to(viewer, source):
        return None
    data = _cached_payload(location, source)
    document = None if data is None else source.find_document(data, document_id, site_scope=site_scope)
    if document is not None:
        return ListedDocument(source, document)
    return next((listed for listed in nested_documents(nested, [source]) if listed.document.document_id == document_id), None)


@dataclass(frozen=True, slots=True)
class ServableDocument:
    """A source document's bytes and how they are served.

    Attributes:
        content: The bytes.
        content_type: The type judged from the bytes, never the upstream's label.
        extension: The filename extension for that type.
    """

    content: bytes
    content_type: str
    extension: str


def servable_document(content: bytes) -> ServableDocument | None:
    """Judge a source document's bytes, alone, as a PDF or an allow-listed raster image (a scan).

    Args:
        content: The document's bytes.

    Returns:
        The document, or None when the bytes are neither.
    """
    if looks_like_pdf(content):
        return ServableDocument(content, PDF_CONTENT_TYPE, "pdf")
    kind = filetype.guess(content)
    if kind is not None and kind.mime.startswith("image/") and kind.mime in INLINE_MEDIA_TYPES:
        return ServableDocument(content, kind.mime, kind.extension)
    return None


def document_bytes(listed: ListedDocument) -> ServableDocument | None:
    """A listed document, from the proxied-bytes cache or its source, only when its bytes are a PDF or image.

    The upstream's declared type is never trusted: the gallery proxy caches whatever it was sent under the same key.

    Args:
        listed: A document :func:`find_listed_document` returned.

    Returns:
        The document, or None when the source could not supply it or its bytes are neither.
    """
    key = listed.source.document_cache_key(listed.document)
    label = f"source document {key}"
    cached = get_or_none(key, label=label)
    if isinstance(cached, tuple) and len(cached) == 2 and isinstance(cached[0], bytes):
        return servable_document(cached[0])
    try:
        content, content_type = listed.source.download_document(listed.document)
    except DocumentUnavailableError:
        logger.debug("Source document %s is unavailable", key, exc_info=True)
        return None
    document = servable_document(content)
    if document is None:
        logger.warning("Source document %s was not a PDF or image (%s); refusing to serve it", key, content_type)
        return None
    set_if_small(key, content, document.content_type, DOCUMENT_CACHE_TTL, label=label, max_bytes=DOCUMENT_MAX_CACHED_BYTES)
    return document
