"""Nominatim plugin: OpenStreetMap place metadata panel on the pin detail page."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind, info_card

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Cached keys that make the panel worth showing at all. A reverse-geocode that
#: matched only an address (no name, no tags) tells a viewer nothing they can't
#: already see on the pin itself, so both surfaces suppress it entirely rather
#: than render an empty card. Kept in step with ``PinController.nominatim_info``'s
#: own list - see this module's ``api_payload``.
_USEFUL_FIELDS = ("website", "phone", "email", "opening_hours", "operator", "wikipedia", "wikidata", "image", "extra_details", "kind_label")


def wikipedia_url(tag_value: str) -> str:
    """Turn OSM's ``wikipedia`` tag into a real article URL.

    The tag is conventionally ``"<lang>:<Article Title>"`` (``"de:Kölner Dom"``),
    but plenty of entries carry a bare title with no language prefix; those are
    English by convention. Returning the wrong host for either form produces a
    dead link, which is worse than no link at all - hence the explicit split
    rather than blind concatenation.

    Args:
        tag_value: The raw OSM ``wikipedia`` tag value.

    Returns:
        The article URL, or ``""`` when the tag is empty.
    """
    if not tag_value:
        return ""
    language, separator, title = tag_value.partition(":")
    if not separator:
        return f"https://en.wikipedia.org/wiki/{tag_value}"
    return f"https://{language}.wikipedia.org/wiki/{title}"


class _SemicolonSplitNameProvider(LocationCacheNameProvider):
    """Splits a semicolon-separated multi-value tag into separate candidates.

    OSM's ``old_name`` tag is often two-or-more former names in one field
    (e.g. ``"Pauline Warfield Lewis Center;Cincinnati State Hospital"``).
    Left as one string, ``sanitize_name`` would strip the semicolon - not
    split on it - running the two names together into one garbled string
    instead of producing two real aliases.
    """

    def candidates(self, location: Location) -> list[str | None]:
        split: list[str | None] = []
        for value in super().candidates(location):
            if isinstance(value, str) and ";" in value:
                split.extend(part.strip() for part in value.split(";") if part.strip())
            else:
                split.append(value)
        return split


class NominatimPanelSource(LocationCachePanelSource):
    """OpenStreetMap Nominatim place metadata for the pin's location."""

    key = "nominatim"
    cache_source = "nominatim"
    section_id = "nominatim-section"
    icon = "map"
    # Distinguishes this panel from the separate "Photon (OpenStreetMap)"
    # panel (plugins.builtin.photon) - both reverse-geocode the same
    # underlying OpenStreetMap data through different, independent hosted
    # services (Nominatim vs. Komoot's Photon), by design, for cross-checking
    # - not a duplicate query against one provider. A bare "OpenStreetMap"
    # title here was ambiguous next to Photon's own title.
    title = "Nominatim"
    # Bespoke markup on the web (its own quick-facts row, wiki chips, hero
    # image), but the underlying data is a plain information card - so the API
    # serves it through the shared INFO contract instead of a Nominatim-shaped
    # response every client would have to special-case.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})

    def fetch(self, pin: Pin) -> None:
        """Reverse-geocode the pin's coordinates and cache the place metadata.

        Nominatim's panel data lands lazily (only once the pin detail page is
        viewed), well after a Location's ``official_name`` is first resolved
        at creation time. When that first resolution never found a real name
        - or, worse, fell back to a bare city/administrative name - this is
        the first opportunity to retry with OpenStreetMap's name in the mix,
        so the name is refreshed here rather than left stuck on a placeholder.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
        from urbanlens.dashboard.services.locations.naming import (
            is_address_derived_name,
            is_meaningful_name,
            update_location_name_from_external_sources,
        )

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        place = NominatimGateway().reverse_geocode(lat, lng)
        LocationCache.set(pin.location, self.cache_source, place or {}, query_key=f"{lat},{lng}")

        location = pin.location
        current_name = location.official_name
        name_needs_improvement = not is_meaningful_name(current_name) or bool(current_name and is_address_derived_name(current_name, location))
        if place and place.get("name") and name_needs_improvement:
            update_location_name_from_external_sources(location, profile=pin.profile)

        if place and place.get("osm_url"):
            self._add_osm_link(pin, location, place["osm_url"])

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The reverse-geocoded OSM place metadata as an information card, or None.

        Applies the same emptiness rule as ``PinController.nominatim_info``
        (see :data:`_USEFUL_FIELDS`): a coordinate-only result is suppressed on
        both surfaces, so a client never has to decide for itself whether a
        card carrying nothing but an address is worth drawing.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            ``{"info": {...}}``, or None when nothing has landed yet or the
            geocode found no metadata worth showing.
        """
        data = self.cached_data(pin)
        if not data or not any(data.get(field) for field in _USEFUL_FIELDS):
            return None

        # Icons match pin_nominatim.html's own quick-facts row, so the two
        # surfaces read as the same panel rather than as two designs.
        facts: list[dict[str, str]] = []
        if data.get("website"):
            facts.append({"icon": "language", "text": data["website"], "href": data["website"]})
        if data.get("phone"):
            facts.append({"icon": "call", "text": data["phone"], "href": f"tel:{data['phone']}"})
        if data.get("email"):
            facts.append({"icon": "mail", "text": data["email"], "href": f"mailto:{data['email']}"})
        # Opening hours and operator are prose, not addresses - no href.
        for key, icon in (("opening_hours", "schedule"), ("operator", "apartment")):
            if data.get(key):
                facts.append({"icon": icon, "text": data[key], "href": ""})

        meta = list(data.get("extra_details") or [])
        if data.get("wikipedia"):
            meta.append({"label": "Wikipedia", "value": data["wikipedia"], "href": wikipedia_url(data["wikipedia"])})
        if data.get("wikidata"):
            meta.append({"label": "Wikidata", "value": data["wikidata"], "href": f"https://www.wikidata.org/wiki/{data['wikidata']}"})

        osm_url = data.get("osm_url") or ""
        return {
            PanelApiKind.INFO.value: info_card(
                heading_name=data.get("name"),
                chips=[data.get("kind_label")],
                facts=facts,
                meta=meta,
                header_link={"url": osm_url, "label": "Open on OpenStreetMap"} if osm_url else None,
                footer_link={"url": osm_url, "label": "View on OpenStreetMap"} if osm_url else None,
                image_url=data.get("image"),
            ),
        }

    @staticmethod
    def _add_osm_link(pin: Pin, location: Location, osm_url: str) -> None:
        """Add the OSM way/node/relation URL to the pin's (and wiki's) links, if not already there.

        Args:
            pin: The pin whose links should include this URL.
            location: The pin's location, for reaching its wiki (if any).
            osm_url: The OSM element URL from the reverse-geocode result.
        """
        from urbanlens.dashboard.services.locations.external_links import add_pin_and_wiki_link

        add_pin_and_wiki_link(pin, location, osm_url, "OpenStreetMap")


class NominatimEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the OSM reverse-geocode cache (a name/alias source) per Location."""

    key: ClassVar[str] = "nominatim"
    verbose_name: ClassVar[str] = "Nominatim"
    cache_source: ClassVar[str] = "nominatim"
    service_keys: ClassVar[tuple[str, ...]] = ("nominatim",)

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Reverse-geocode a location's coordinates via Nominatim.

        Args:
            location: The location to reverse-geocode.

        Returns:
            Tuple of (place payload or None, coordinate query key).
        """
        from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        place = NominatimGateway().reverse_geocode(lat, lng)
        return place, f"{lat},{lng}"


class NominatimPlugin(UrbanLensPlugin):
    """OpenStreetMap place metadata for pinned locations."""

    name: ClassVar[str] = "nominatim"
    verbose_name: ClassVar[str] = "Nominatim"
    description: ClassVar[str] = "Reverse-geocodes pins via Nominatim and shows OpenStreetMap place metadata on the pin detail page."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Nominatim API."""
        return {
            "nominatim": ServiceDefaults(
                display_name="Nominatim (OpenStreetMap)",
                calls_per_minute=1,
                calls_per_day=500,
                notes="Free API. Hard limit: 1 req/second per OSM ToS.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the OpenStreetMap pin-detail panel."""
        return [NominatimPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the reverse-geocoded OSM place name, and its former name(s), as candidates."""
        return [
            LocationCacheNameProvider(source="nominatim", cache_source="nominatim", keys=("name",), verbose_name="OpenStreetMap"),
            _SemicolonSplitNameProvider(source="nominatim_old_name", cache_source="nominatim", keys=("old_name",), verbose_name="OpenStreetMap"),
        ]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the OSM reverse-geocode cache to scheduled background enrichment."""
        return [NominatimEnrichmentSource()]
