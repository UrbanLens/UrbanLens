"""Nearby reference documents plugin: the Wikipedia articles and Wikidata entities about a place, on Article > Sources.

REData's ``reference-documents/`` is the only coordinate search among its reference sources; the archives the Media
gallery searches by name (``media_archives``) are reached through ``reference-documents/search/``. A document is listed
only when it is about the place - placed inside it, or naming it - since a kilometre's radius holds every article about
the neighbourhood. The article the Wikipedia panel already matched to the place is left out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.pins.external_data import DocumentPanelSource, DocumentUnavailableError, SourceDocument

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.media.subject_relevance import MediaSubject
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: The ``ReferenceDocumentSerializer`` fields a listing is built from.
_STORED_FIELDS = ("provider", "external_id", "kind", "title", "description", "url", "date_text", "creator", "license", "latitude", "longitude", "distance_meters")

#: The Wikidata claims worth a reader's glance, in the order they are shown.
_WIKIDATA_CLAIMS = ("instance_of", "architectural_style", "heritage_designation")

_PROVIDER_NAMES = {"wikipedia": "Wikipedia", "wikidata": "Wikidata"}


def _stored_document(row: dict[str, Any]) -> dict[str, Any]:
    stored = {name: row.get(name) for name in _STORED_FIELDS if row.get(name) not in (None, "")}
    attributes = row.get("attributes")
    if isinstance(attributes, dict):
        claims = {name: attributes[name] for name in ("pageid", *_WIKIDATA_CLAIMS) if attributes.get(name)}
        if claims:
            stored["attributes"] = claims
    return stored


def _matched_article(location: Location) -> tuple[str, Any]:
    """The Wikipedia panel's matched article for ``location``, as ``(url, page id)``; empty when it has none."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    row = LocationCache.get_fresh(location, "wikipedia")
    data = row.data if row is not None and isinstance(row.data, dict) else {}
    return str(data.get("url") or ""), data.get("page_id")


def _is_matched(document: dict[str, Any], matched_url: str, matched_page_id: Any) -> bool:
    if document.get("provider") != "wikipedia":
        return False
    attributes = document.get("attributes") or {}
    if matched_page_id is not None and attributes.get("pageid") == matched_page_id:
        return True
    return bool(matched_url) and str(document.get("url") or "").rstrip("/") == matched_url.rstrip("/")


def _subject_line(document: dict[str, Any]) -> str:
    """What the listing says under a document's title: where it comes from and what it says about the place."""
    provider = str(document.get("provider") or "")
    parts = [_PROVIDER_NAMES.get(provider, provider.replace("_", " ").title())]
    if provider == "wikidata":
        attributes = document.get("attributes") or {}
        parts.extend(str(attributes[name]) for name in _WIKIDATA_CLAIMS if attributes.get(name))
        # Wikidata's inception and architect claims, which REData promotes to these two fields.
        if date_text := str(document.get("date_text") or ""):
            parts.append(f"inception {date_text[:4]}")
        if creator := str(document.get("creator") or ""):
            parts.append(f"architect {creator}")
    distance = document.get("distance_meters")
    if isinstance(distance, int | float) and not isinstance(distance, bool):
        parts.append(f"{round(distance):,} m away")
    return " · ".join(part for part in parts if part)


class NearbyReferenceDocumentsSource(DocumentPanelSource):
    """Wikipedia articles and Wikidata entities placed at, or naming, the pin's place."""

    key = "redata_reference_near"
    cache_source = "redata_reference_near"
    icon = "menu_book"
    title = "Wikipedia & Wikidata"
    documents_depend_on_site_scope: ClassVar[bool] = False
    documents_judged: ClassVar[bool] = True

    def gate(self, pin: Pin) -> bool:
        """Requires coordinates and REData."""
        return bool(pin.effective_latitude and pin.effective_longitude) and redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache the documents placed near the pin, unless no source answered."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.locations.redata_point_data import point_key, reference_documents_near

        latitude = float(pin.effective_latitude or 0)
        longitude = float(pin.effective_longitude or 0)
        envelope = reference_documents_near(latitude, longitude)
        if not envelope.complete and not envelope.results:
            return
        matched_url, matched_page_id = _matched_article(pin.location)
        documents = [stored for row in envelope.results if not _is_matched(stored := _stored_document(row), matched_url, matched_page_id) and stored.get("url")]
        LocationCache.set(pin.location, self.cache_source, envelope.marked({"documents": documents}), query_key=point_key(latitude, longitude))

    def may_list_documents(self, data: dict) -> bool:
        """Whether the cached payload holds any document."""
        return bool((data or {}).get("documents"))

    def source_documents(self, data: dict, *, site_scope: bool, subject: MediaSubject | None = None) -> list[SourceDocument]:
        """The cached documents about ``subject``, nearest first; none without a subject to judge them against."""
        from urbanlens.dashboard.models.images.relevance import media_item_key
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        if subject is None:
            return []
        listed: list[SourceDocument] = []
        for document in (data or {}).get("documents") or []:
            if not isinstance(document, dict) or not (url := str(document.get("url") or "")):
                continue
            title = str(document.get("title") or url)
            latitude, longitude = document.get("latitude"), document.get("longitude")
            candidate = MediaItem(
                url=url,
                thumb_url="",
                caption=title,
                source=self.title,
                title=title,
                description=str(document.get("description") or ""),
                latitude=float(latitude) if isinstance(latitude, int | float) else None,
                longitude=float(longitude) if isinstance(longitude, int | float) else None,
            )
            if subject.matches(candidate):
                listed.append(SourceDocument(document_id=media_item_key(url), title=title, content_type="text/html", subject=_subject_line(document), page_url=url))
        return listed

    def download_document(self, document: SourceDocument) -> tuple[bytes, str]:
        """Never proxied: an article or entity is read on its own page.

        Raises:
            DocumentUnavailableError: Always.
        """
        raise DocumentUnavailableError(document.document_id)


class NearbyReferenceDocumentsPlugin(UrbanLensPlugin):
    """Wikipedia and Wikidata material about a pinned place, sourced through REData."""

    name: ClassVar[str] = "redata_nearby_documents"
    verbose_name: ClassVar[str] = "Wikipedia & Wikidata Sources"
    description: ClassVar[str] = "Lists the Wikipedia articles and Wikidata entities placed at, or naming, a pin's place under Article > Sources, with Wikidata's construction date, architect and heritage designation."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the Sources listing."""
        return [NearbyReferenceDocumentsSource()]
