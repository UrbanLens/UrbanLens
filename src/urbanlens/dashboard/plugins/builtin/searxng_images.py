"""Web Images plugin: a Media-gallery tab of web-image search results, via REData.
So the query is built as required, disambiguating ``OR``-groups that a general image engine treats as required clauses (see :func:`build_image_query`):"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Fixed subject-matter clause: at least one of these words must appear, so a place name that
#: coincides with an operating business/brand doesn't flood the gallery with irrelevant marketing
#: imagery.
SUBJECT_TERMS: tuple[str, ...] = ("abandoned", "urbex", "urban exploration", "decay", "vacant", "postcard")

#: Country names (case-insensitive) treated as "United States" when deciding
#: whether the broad geographic term should be the state or the country.
_US_COUNTRY_NAMES: frozenset[str] = frozenset({"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america", "america"})

_MAX_IMAGES = 30


def _clean_term(value: str | None) -> str:
    """Normalise one query term: trim, and drop embedded quotes that would break grouping."""
    if not value:
        return ""
    return value.replace('"', "").strip()


def _dedup_preserving_order(terms: list[str]) -> list[str]:
    """Drop empty and case-insensitively duplicate terms, keeping first occurrence and order."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        cleaned = _clean_term(term)
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def assemble_image_query(aliases: list[str], area_terms: list[str], ancestor_terms: list[str] | None = None) -> str | None:
    """Assemble the grouped SearXNG relevance query from its component terms.
    Kept separate from :func:`build_image_query` (which pulls the terms off a ``Pin``) so the pure string-assembly logic is unit-testable without the ORM.

    Args:
        aliases: The place's names (already nickname-filtered).
        area_terms: Geographic disambiguators (state/country + municipality).
        ancestor_terms: Names of a child pin's parent site (see ``Pin.ancestor_search_names``).

    Returns:
        A query string of ``OR``-grouped, quoted clauses (e.g. ``("A" OR "B") ("NY" OR "Troy") ("abandoned" OR ...)``), or ``None`` when no usable alias remained."""
    alias_terms = _dedup_preserving_order(aliases)
    if not alias_terms:
        return None

    groups = [_or_group(alias_terms)]
    ancestors = _dedup_preserving_order(ancestor_terms or [])
    if ancestors:
        groups.append(_or_group(ancestors))
    area = _dedup_preserving_order(area_terms)
    if area:
        groups.append(_or_group(area))
    groups.append(_or_group(list(SUBJECT_TERMS)))
    return " ".join(groups)


def _or_group(terms: list[str]) -> str:
    """Render one parenthesised, quoted ``OR`` group, e.g. ``("A" OR "B")``."""
    return "(" + " OR ".join(f'"{term}"' for term in terms) + ")"


def build_image_query(pin: Pin) -> str | None:
    """Build the aggressive image-search query for a pin, or ``None`` if unbuildable.

    Args:
        pin: The pin whose place is being searched.

    Returns:
        The grouped query string, or ``None`` when the pin has no meaningful name to search on (the provider then stays quietly absent)."""
    from urbanlens.dashboard.models.aliases.model import AliasType
    from urbanlens.dashboard.models.wiki.model import Wiki

    aliases: list[str] = []
    for name in (pin.meaningful_official_name, pin.meaningful_name):
        if name:
            aliases.append(name)
    aliases.extend(pin.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))
    if pin.location_id is not None:
        wiki = Wiki.objects.filter(location_id=pin.location_id).first()
        if wiki is not None:
            if wiki.name:
                aliases.append(wiki.name)
            aliases.extend(wiki.aliases.exclude(kind=AliasType.NICKNAME).values_list("name", flat=True))

    return assemble_image_query(aliases, _area_terms(pin), pin.ancestor_search_names())


def _area_terms(pin: Pin) -> list[str]:
    """Geographic disambiguators: the broad region plus the municipality.

    Args:
        pin: The pin whose location supplies the geographic fields.

    Returns:
        Zero to two area terms, most-broad first."""
    country = (pin.effective_country or "").strip()
    is_usa = not country or country.casefold() in _US_COUNTRY_NAMES
    broad = pin.effective_state if is_usa else country
    municipality = pin.effective_city or pin.effective_county
    return [term for term in (broad, municipality) if term]


class SearxngImageMediaSource(GalleryMediaSource):
    """Web-image search results for a pin's place, via REData's web-search endpoint."""

    key = "searxng_images"
    cache_source = "searxng_images"
    icon = "travel_explore"
    title = "Web Images"

    def gate(self, pin: Pin) -> bool:
        """Needs REData configured and a buildable relevance query."""
        return redata_configured() and build_image_query(pin) is not None

    def fetch(self, pin: Pin) -> None:
        """Run the REData image search for the pin's relevance query and cache it."""
        import logging

        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway

        query = build_image_query(pin)
        results: list[dict] = []
        if query:
            try:
                results = RedataSearchGateway().search_web(query, images=True, max_results=_MAX_IMAGES)
            except LocationContextUnavailableError as exc:
                # An outage must not be written to the cache.
                logging.getLogger(__name__).warning("REData image search failed for %r, leaving it unfetched to retry: %s", query, exc)
                return
        LocationCache.set(pin.location, self.cache_source, {"items": results, "query": query or ""}, query_key=query or "")

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from the cached REData image results."""
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        items = (data or {}).get("items") or []
        return [
            MediaItem(
                url=item["thumbnail"],
                thumb_url=item["thumbnail"],
                caption=item.get("title") or "",
                source="Web Search",
                page_url=item.get("link") or item["thumbnail"],
            )
            for item in items[:_MAX_IMAGES]
            if item.get("thumbnail")
        ]


class SearxngImagesPlugin(UrbanLensPlugin):
    """Adds a broad web-image search tab (via REData) to the Media gallery."""

    name: ClassVar[str] = "searxng_images"
    verbose_name: ClassVar[str] = "Web Images"
    description: ClassVar[str] = "Adds a Web Images tab to the pin detail and wiki Media galleries, sourced from an aggressive, relevance-shaped image search via REData's web-search endpoint."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the SearXNG web-image Media-gallery provider."""
        return [SearxngImageMediaSource()]
