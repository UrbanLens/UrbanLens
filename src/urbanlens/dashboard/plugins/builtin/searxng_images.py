"""SearXNG Images plugin: a Media-gallery tab of web-image search results.

Unlike the archive providers (Wikimedia, Smithsonian, Library of Congress),
which search one curated collection, this provider casts the widest net: it
runs an aggressive, relevance-shaped query across many image engines behind a
self-hosted SearXNG instance to surface photos of a place that never made it
into a formal archive - the abandoned-hospital shots, urbex galleries, and
vintage postcards that live on Flickr, imgur, Pinterest, DeviantArt, etc.

The whole value here is *precision*: a bare "Hudson River State Hospital"
image search returns unrelated stock photos and same-named places elsewhere.
So the query is built as three ``OR``-groups that a general image engine
treats as required, disambiguating clauses (see :func:`build_image_query`):

* **Aliases** - every non-nickname name the place is known by, quoted. A
  nickname is a private label ("the spooky hospital") that no external source
  indexes, so it's excluded.
* **Area** - the state (US) or country (elsewhere) *and* the municipality,
  quoted. Requiring a geographic clause rejects the same-named place two
  states over.
* **Subject** - the site's fixed subject vocabulary (abandoned, urbex,
  decay, ...), so a generic name doesn't pull in the operating business of
  the same name.

Shares the ``searxng`` service_key (rate limiting / call logging) with the
web-search :class:`~urbanlens.dashboard.services.apis.search.searxng.SearxngGateway`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.external_data import GalleryMediaSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.external_data import PanelSource

#: Fixed subject-matter clause: at least one of these words must appear, so a
#: place name that coincides with an operating business/brand doesn't flood the
#: gallery with irrelevant marketing imagery. These describe UrbanLens's own
#: subject (urban exploration / abandoned places), not any one location.
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


def assemble_image_query(aliases: list[str], area_terms: list[str]) -> str | None:
    """Assemble the grouped SearXNG relevance query from its component terms.

    Kept separate from :func:`build_image_query` (which pulls the terms off a
    ``Pin``) so the pure string-assembly logic is unit-testable without the ORM.

    Args:
        aliases: The place's names (already nickname-filtered). Required - an
            empty list yields ``None``, since there is nothing to search for.
        area_terms: Geographic disambiguators (state/country + municipality).
            Optional; an empty list simply omits the area group.

    Returns:
        A query string of ``OR``-grouped, quoted clauses (e.g.
        ``("A" OR "B") ("NY" OR "Troy") ("abandoned" OR ...)``), or ``None``
        when no usable alias remained.
    """
    alias_terms = _dedup_preserving_order(aliases)
    if not alias_terms:
        return None

    groups = [_or_group(alias_terms)]
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

    Aliases are gathered from the pin's own canonical names, its non-nickname
    :class:`PinAlias` rows, and - when the location has a community wiki - the
    wiki's non-nickname aliases, so the query benefits from names other users
    have contributed for the same place.

    Args:
        pin: The pin whose place is being searched.

    Returns:
        The grouped query string, or ``None`` when the pin has no meaningful
        name to search on (the provider then stays quietly absent).
    """
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

    return assemble_image_query(aliases, _area_terms(pin))


def _area_terms(pin: Pin) -> list[str]:
    """Geographic disambiguators: the broad region plus the municipality.

    Broad term is the US state (for a US or country-less pin) or the country
    (elsewhere); the tighter term is the city, falling back to the county. The
    example ``("New York" OR "poughkeepsie")`` is state + city.

    Args:
        pin: The pin whose location supplies the geographic fields.

    Returns:
        Zero to two area terms, most-broad first.
    """
    country = (pin.effective_country or "").strip()
    is_usa = not country or country.casefold() in _US_COUNTRY_NAMES
    broad = pin.effective_state if is_usa else country
    municipality = pin.effective_city or pin.effective_county
    return [term for term in (broad, municipality) if term]


class SearxngImageMediaSource(GalleryMediaSource):
    """Web-image search results for a pin's place, via SearXNG's image engines."""

    key = "searxng_images"
    cache_source = "searxng_images"
    icon = "travel_explore"
    title = "Web Images"

    def gate(self, pin: Pin) -> bool:
        """Needs a configured SearXNG instance and a buildable relevance query."""
        from urbanlens.UrbanLens.settings.app import settings

        return bool(settings.searxng_base_url) and build_image_query(pin) is not None

    def fetch(self, pin: Pin) -> None:
        """Run the SearXNG image search for the pin's relevance query and cache it."""
        import logging

        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.search.searxng import SearxngError, SearxngGateway

        query = build_image_query(pin)
        results: list[dict] = []
        if query:
            try:
                results = SearxngGateway().search_images(query, max_results=_MAX_IMAGES)
            except SearxngError as exc:
                # A misconfigured or unreachable instance degrades to "no
                # results" rather than failing the whole Media gallery loader.
                logging.getLogger(__name__).warning("SearXNG image search failed for %r: %s", query, exc)
        LocationCache.set(pin.location, self.cache_source, {"items": results, "query": query or ""}, query_key=query or "")

    def media_items(self, data: dict) -> list[MediaItem]:
        """Rebuild ``MediaItem``s from the cached SearXNG image results."""
        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        items = (data or {}).get("items") or []
        return [
            MediaItem(
                url=item["url"],
                thumb_url=item.get("thumbnail") or item["url"],
                caption=item.get("title") or "",
                source=item.get("source") or "Web Search",
                page_url=item.get("page_url") or item["url"],
            )
            for item in items[:_MAX_IMAGES]
            if item.get("url")
        ]


class SearxngImagesPlugin(UrbanLensPlugin):
    """Adds a broad web-image search tab (via SearXNG) to the Media gallery."""

    name: ClassVar[str] = "searxng_images"
    verbose_name: ClassVar[str] = "SearXNG Images"
    description: ClassVar[str] = "Adds a Web Images tab to the pin detail and wiki Media galleries, sourced from an aggressive, relevance-shaped image search across many engines via SearXNG. Requires UL_SEARXNG_BASE_URL."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the SearXNG web-image Media-gallery provider."""
        return [SearxngImageMediaSource()]
