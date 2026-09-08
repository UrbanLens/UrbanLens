"""Reference documents nearby plugin: Wikipedia articles and Wikidata claims near a pin, via REData.

Wires up REData's ``GET /api/v1/reference-documents/`` (``../REData/docs/api-reference.md``,
"GET /reference-documents/ - archival material about a coordinate") - distinct from
``/reference-documents/search/``, which the Media gallery's four name-searched archives
(Smithsonian, Library of Congress, Internet Archive, Digital Commonwealth) already consume
through ``plugins.builtin.media_archives`` and share
``services.apis.locations.redata_reference_documents_gateway`` with this panel, but never
touch the near-a-coordinate half of that module.

Two providers, both geosearchable so neither needs a place name:

- ``wikipedia`` - an article's intro extract, prose someone chose to write.
- ``wikidata`` - structured claims about the same kind of entity: what it *is*, when it was
  built, who designed it, its architectural style and heritage designation. Per REData's own
  docs this is "frequently the more useful half for property research" - it is genuinely
  novel data no other panel in this app surfaces, since the state/city inventories behind
  ``redata_historic_registers`` cover only places some body has formally designated, and
  Wikidata's claims cover anything anyone has bothered to catalogue.

Only the *nearest* article and the *nearest* claims-bearing entity are shown, mirroring
``WikipediaPanelSource``'s own "best match, not every match" choice - a coordinate can
return several unrelated Wikidata entities (a building, a nearby statue, a transit stop),
and averaging or listing all of them would say less than picking the one actually at this
point.

**Gated behind ``SiteFeature.PLACES``** (decided 2026-09-08): unlike a panel about the pin's
own place, a near-a-coordinate search is inherently about *something else nearby* - the
Wikipedia article and Wikidata entity are their own thing, not necessarily the pin's subject.
Reuses the flag that already gates the map's Places layer for this same provider
(``places_wikipedia_enabled`` in ``services.profile.profile_settings``), rather than
``NEARBY_RESEARCH``: this is the same Wikipedia data by the same product concept, just shown
on the pin page instead of as a map marker.
"""

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
        """The nearest article's title/link plus the nearest claims-bearing entity's facts.

        A Wikidata entity with no claims at all (its enrichment query degraded - see
        ``RedataReferenceDocumentsGateway.get_reference_documents``, or the entity simply
        has none of the properties this panel tracks) contributes nothing here rather than
        an empty-valued row; the card still renders on the Wikipedia half alone in that
        case, and on neither only when both are truly empty.
        """
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
        """The rendered card, plus the nearest Wikipedia article's intro extract as ``description``.

        ``description`` isn't part of ``render_context``'s contract - the web template
        (``_simple_info_panel.html``) has no slot for a paragraph of prose, and the
        existing ``wikipedia`` panel already shows the extract there - so it is added here
        rather than threaded through ``render_context``, matching how ``NpsPanelSource``
        adds fields the shared web template can't use.
        """
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
        """Rate-limit defaults for REData's reference-documents endpoints.

        Shared with the Media gallery's four archive-search providers
        (``plugins.builtin.media_archives``): both this panel's near-a-coordinate calls and
        their name searches go through the same ``RedataReferenceDocumentsGateway`` class,
        whose ``service_key`` (and so its rate-limit budget) is one value regardless of
        which of its two endpoints is called - see that module's docstring. This is the
        first plugin to declare it; without a declaration it would silently fall back to
        the generic 20/min-500/day default with no notes.
        """
        return {
            "redata_reference_documents": ServiceDefaults(
                display_name="REData Reference Documents",
                # Shares REData's single 1,000 req/hour "lookup" pool with geocode/weather/
                # historical-features/etc. (see REData's own api-reference.md, "Rate
                # limiting") - one call per pin-detail panel render, the same low-volume
                # shape as the sibling redata_historical_features panel.
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
