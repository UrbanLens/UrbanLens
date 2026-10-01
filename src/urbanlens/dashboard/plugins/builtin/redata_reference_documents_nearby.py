"""Reference documents nearby plugin: Wikipedia articles and Wikidata claims near a pin, via REData.
Two providers, both geosearchable so neither needs a place name:"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import info_card_from_render_context
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Wikidata claim fields promoted to meta rows, in display order - REData's own
#: ``ReferenceDocumentSerializer`` field names (``date_text``/``creator`` are promoted
#: columns; the rest live in ``attributes``, per ``WikidataGateway.CLAIM_PROPERTIES``).
_CLAIM_META: tuple[tuple[str, str], ...] = (
    ("Built", "date_text"),
    ("Designer", "creator"),
    ("Style", "architectural_style"),
    ("Heritage status", "heritage_designation"),
)


def _nearest(documents: list[Any], provider: str) -> dict[str, Any] | None:
    """The first (nearest - REData returns each provider's own rows distance-sorted)
    row from ``provider``, or None."""
    for document in documents:
        if isinstance(document, dict) and document.get("provider") == provider:
            return document
    return None


def _claim_value(entity: dict[str, Any], field: str) -> str:
    """A claim field's value, whether it's a promoted column or lives in ``attributes``."""
    value = entity.get(field) if field in entity else (entity.get("attributes") or {}).get(field)
    return str(value or "").strip()


class ReferenceDocumentsNearbyPanelSource(RedataInfoPanelSource):
    """The nearest Wikipedia article and Wikidata entity near the pin, from REData."""

    key = "redata_reference_documents_nearby"
    cache_source = "redata_reference_documents_nearby"
    section_id = "reference-documents-nearby-section"
    icon = "auto_stories"
    title = "Reference Documents"
    required_feature: ClassVar[SiteFeature | None] = SiteFeature.PLACES

    payload_key: ClassVar[str] = "documents"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Wikipedia + Wikidata results within REData's default search radius of the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import RedataReferenceDocumentsGateway

        return RedataReferenceDocumentsGateway().get_reference_documents(latitude, longitude)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """The nearest article's title/link plus the nearest claims-bearing entity's facts."""
        documents = (data or {}).get(self.payload_key) or []
        article = _nearest(documents, "wikipedia")
        entity = _nearest(documents, "wikidata")

        chips: list[str] = []
        meta: list[dict[str, str]] = []
        if entity is not None:
            if instance_of := _claim_value(entity, "instance_of"):
                chips.append(instance_of)
            meta = [{"label": label, "value": value} for label, field in _CLAIM_META if (value := _claim_value(entity, field))]

        heading_name = (article or entity or {}).get("title") or None
        footer_link = {"url": article["url"], "label": "View on Wikipedia"} if article and article.get("url") else None

        if not (heading_name or chips or meta or footer_link):
            return None
        return {"heading_name": heading_name, "chips": chips, "meta": meta, "footer_link": footer_link}

    def api_info(self, pin: Pin, data: dict) -> dict[str, Any] | None:
        """The rendered card, plus the nearest Wikipedia article's intro extract as ``description``."""
        context = self.render_context(pin, data)
        if context is None:
            return None
        card = info_card_from_render_context(context)
        article = _nearest((data or {}).get(self.payload_key) or [], "wikipedia")
        card["description"] = (article or {}).get("description") or None
        return card


class ReferenceDocumentsNearbyPlugin(UrbanLensPlugin):
    """Wikipedia articles and Wikidata claims near pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_reference_documents_nearby"
    verbose_name: ClassVar[str] = "Reference Documents Nearby"
    description: ClassVar[str] = (
        "Shows the nearest Wikipedia article and Wikidata entity for a pin's location - construction date, architect, style and heritage designation where Wikidata has them - sourced through REData's reference-documents near-a-coordinate endpoint."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's reference-documents endpoints."""
        return {
            "redata_reference_documents": ServiceDefaults(
                display_name="REData Reference Documents",
                calls_per_minute=20,
                calls_per_day=None,
                notes=(
                    "Archival/encyclopaedic lookups via GET /reference-documents/ (near-coordinate, this panel) "
                    "and GET /reference-documents/search/ (by name, the Media gallery's archive providers). "
                    "See services.apis.locations.redata_reference_documents_gateway."
                ),
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the reference-documents-nearby pin-detail panel."""
        return [ReferenceDocumentsNearbyPanelSource()]
