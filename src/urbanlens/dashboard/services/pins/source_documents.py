"""Documents listed under Article > Sources, gathered from every :class:`DocumentPanelSource` cached for a location."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.core.bounded_cache import get_or_none, set_if_small
from urbanlens.dashboard.services.media.proxied_media import looks_like_pdf
from urbanlens.dashboard.services.pins.external_data import DocumentPanelSource, DocumentUnavailableError, SourceDocument, document_panel_sources, get_panel_source, panel_visible_to

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.auth.models import AnonymousUser

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)

DOCUMENT_CACHE_TTL = 3600
#: Scanned inventory forms run to several megabytes; sized like the gallery proxy's ceiling so both share entries.
DOCUMENT_MAX_CACHED_BYTES = 4 * 1024 * 1024


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


def _cached_payload(location: Location, source: DocumentPanelSource) -> dict | None:
    """A source's fresh cached payload for ``location``, or None when nothing fresh has landed."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, source.cache_source)
    return None if row is None else (row.data or {})


def collect_source_documents(location: Location, *, viewer: AbstractBaseUser | AnonymousUser, driver: Pin | None, site_scope: bool, may_fetch: bool) -> SourceListing:
    """Gather every visible source's documents for a location, scheduling a fetch for any source not ready yet.

    Args:
        location: The location whose cache rows are read.
        viewer: The requesting user; feature-gated sources are skipped for a viewer without the feature.
        driver: The pin a fetch runs as (its owner's quota and ``external_apis_enabled``), or None when there is none.
        site_scope: Whether the page describes a parcel/site rather than one building.
        may_fetch: False once the caller's poll budget is spent.

    Returns:
        The listing. A source that is fetching contributes nothing yet; one that cannot fetch lists whatever it has.
    """
    from urbanlens.dashboard.services.pins import external_data

    documents: list[ListedDocument] = []
    pending = False
    for source in document_panel_sources():
        if not panel_visible_to(viewer, source):
            continue
        data = _cached_payload(location, source)
        if data is None or not source.documents_ready(data, site_scope=site_scope):
            if may_fetch and driver is not None and source.gate(driver) and external_data.schedule_panel_fetch(source.key, driver):
                pending = True
                continue
            if data is None:
                continue
        documents.extend(ListedDocument(source, document) for document in source.source_documents(data, site_scope=site_scope))
    return SourceListing(documents=documents, pending=pending)


def warm_site_scope_documents(pin: Pin) -> None:
    """Fetch every document source not yet ready at site scope, for a pin that has just become a site.

    Args:
        pin: The pin now describing a site.
    """
    from urbanlens.dashboard.services.pins import external_data

    for source in document_panel_sources():
        data = _cached_payload(pin.location, source)
        if data is not None and source.documents_ready(data, site_scope=True):
            continue
        if source.gate(pin):
            external_data.schedule_panel_fetch(source.key, pin)


def find_listed_document(location: Location, source_key: str, document_id: str, *, viewer: AbstractBaseUser | AnonymousUser, site_scope: bool) -> ListedDocument | None:
    """The document with this id, only when the location's cached payload lists it for this viewer and scope.

    Args:
        location: The location whose cache rows are read.
        source_key: The panel source key from the URL.
        document_id: The document id from the URL.
        viewer: The requesting user.
        site_scope: Whether the page describes a parcel/site rather than one building.

    Returns:
        The listed document, or None.
    """
    source = get_panel_source(source_key)
    if not isinstance(source, DocumentPanelSource) or not panel_visible_to(viewer, source):
        return None
    data = _cached_payload(location, source)
    if data is None:
        return None
    document = source.find_document(data, document_id, site_scope=site_scope)
    return None if document is None else ListedDocument(source, document)


def pdf_bytes(listed: ListedDocument) -> bytes | None:
    """A listed document's bytes, from the proxied-bytes cache or its source, only when they are a PDF.

    Args:
        listed: A document :func:`find_listed_document` returned.

    Returns:
        The bytes, or None when the source could not supply them or they are not a PDF.
    """
    key = listed.source.document_cache_key(listed.document)
    label = f"source document {key}"
    cached = get_or_none(key, label=label)
    if isinstance(cached, tuple) and len(cached) == 2 and isinstance(cached[0], bytes):
        content = cached[0]
    else:
        try:
            content, content_type = listed.source.download_document(listed.document)
        except DocumentUnavailableError:
            logger.debug("Source document %s is unavailable", key, exc_info=True)
            return None
        if not looks_like_pdf(content):
            logger.warning("Source document %s was not a PDF (%s); refusing to serve it", key, content_type)
            return None
        set_if_small(key, content, content_type, DOCUMENT_CACHE_TTL, label=label, max_bytes=DOCUMENT_MAX_CACHED_BYTES)
    return content if looks_like_pdf(content) else None
