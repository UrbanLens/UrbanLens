"""Wikipedia service - finds and verifies articles for a pin's location."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

from django.utils.html import escape

# only ever parses markup already run through nh3.clean() (or html.escape()), which strips doctypes/entities/attributes before lxml sees it
import lxml.html as lxml_html  # nosec B410
import nh3

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_coordinate

logger = logging.getLogger(__name__)

_GEO_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_RADIUS_METERS = 500
_MAX_CANDIDATES = 5
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"

# The REST summary endpoint (`_fetch_summary`) truncates well below the
# article's actual lead section, and the lead section itself is often not
# enough to fill the card's available space either (e.g. a cemetery article
# whose lead is a couple hundred words but whose body has several more
# sections of real content). Below this length, pull a much bigger slice of
# the whole article body instead of just the lead, so the frontend has real
# margin to work with regardless of how tall its card ends up (see the
# client-side clamp in dashboard/pages/location/index.html).
_SHORT_EXTRACT_CHARS = 1200
# Server-side cap on the extended extract - generous, but bounded so a single
# huge article (some run 50k+ characters) doesn't get pulled in wholesale.
# Measured in visible text characters, not markup bytes.
_EXTENDED_EXTRACT_CHARS = 5000

# `prop=extracts` (without `explaintext`) returns the article's real parsed
# markup instead of flattened plain text, so headings/paragraphs/lists survive.
# That markup is untrusted (it's from an external API), so it's sanitized down
# to this small allowlist before anything else touches it. Links are dropped
# (unwrapped to plain text) rather than allowed through, since MediaWiki's
# internal hrefs are relative and meaningless outside Wikipedia.
_ALLOWED_TAGS = frozenset(
    {
        "p",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "b",
        "i",
        "em",
        "strong",
        "sup",
        "sub",
        "blockquote",
        "br",
    }
)
# Tags whose contents are dropped along with the tag itself, rather than
# unwrapped - these are non-prose widgets (embedded population-graph SVGs,
# stray <style>/<script>/<table> blocks) whose inner text would otherwise leak
# into the card as noise (chart axis labels, raw JSON, etc.).
_CLEAN_CONTENT_TAGS = frozenset({"wiki-chart", "svg", "style", "script", "table"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Sections whose content reads poorly as prose (bibliography entries, bare
# citation text, etc.) - the extended extract is truncated at the first
# heading matching one of these rather than including them.
_STOP_HEADING_TITLES = frozenset(
    {
        "references",
        "external links",
        "see also",
        "notes",
        "bibliography",
        "further reading",
        "sources",
        "gallery",
        "footnotes",
    }
)


@dataclass(slots=True, kw_only=True)
class WikipediaGateway(Gateway):
    """
    Fetches Wikipedia article summaries for a geographic location.

    The address-verification step ensures we return an article that is actually
    about the queried address rather than a nearby unrelated landmark.
    """

    service_key: ClassVar[str] = "wikipedia"
    paid_service: ClassVar[bool] = False

    base_url: str = _GEO_SEARCH_URL

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def get_nearby_articles(
        self,
        latitude: float,
        longitude: float,
        radius_m: int = 5000,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """Return Wikipedia articles near the given coordinates as place dicts.

        Unlike ``get_article_for_location``, this method skips address verification
        and is intended for map-layer use where quantity and proximity matter more
        than exact address matching.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            radius_m: Search radius in metres (max 10 000 per Wikipedia API).
            limit: Maximum articles to return.

        Returns:
            List of place dicts compatible with the Places layer marker format.
            Each has: ``place_id``, ``name``, ``lat``, ``lng``, ``source``,
            ``description``, ``url``, ``types``, ``rating``, ``vicinity``.
        """
        params: dict[str, str | int] = {
            "action": "query",
            "list": "geosearch",
            "gscoord": f"{latitude}|{longitude}",
            "gsradius": min(radius_m, 10_000),
            "gslimit": limit,
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("query", {}).get("geosearch", [])
        except Exception:
            logger.exception("Wikipedia nearby search failed for %s,%s", redact_coordinate(latitude), redact_coordinate(longitude))
            return []

        places = []
        for item in results:
            title = item.get("title", "")
            page_id = item.get("pageid")
            if not title or page_id is None:
                continue
            places.append(
                {
                    "place_id": f"wiki_{page_id}",
                    "name": title,
                    "lat": item.get("lat"),
                    "lng": item.get("lon"),
                    "source": "wikipedia",
                    "description": "",
                    "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                    "types": ["wikipedia"],
                    "rating": None,
                    "vicinity": "",
                }
            )
        return places

    def get_article_for_location(
        self,
        latitude: float,
        longitude: float,
        address_components: dict[str, str],
        name: str = "",
    ) -> dict[str, Any] | None:
        """
        Find a Wikipedia article near the coordinates that matches the place.

        Args:
            latitude: WGS-84 latitude of the location.
            longitude: WGS-84 longitude of the location.
            address_components: Dict with optional keys 'locality', 'route',
                'street_number', 'administrative_area_level_1'.
            name: The place's own name (e.g. pin/wiki name), when known.
                Checked against each candidate's title first, since a title
                match is a far stronger signal than an address mention -- a
                same-block article that happens to reference the street or
                city is not necessarily the article for this specific place.

        Returns:
            A dict with keys ``title``, ``extract``, ``url``, ``thumbnail``,
            ``description``, ``page_id`` - or None if no matching article found.
        """
        candidates = self._geo_search(latitude, longitude)
        for candidate in candidates:
            summary = self._fetch_summary(candidate["title"])
            if summary and self._address_matches(summary, address_components, name):
                article = self._normalise(summary)
                self._fill_short_extract(article, candidate["title"])
                return article
        return None

    # -- private ----------------------------------------------------------------

    def _fill_short_extract(self, article: dict[str, Any], title: str) -> None:
        """Mutate ``article["extract"]`` in place with more text when it's short.

        The lead section alone is frequently not enough content to fill a
        card's available space, so this pulls from later sections of the
        article body too rather than stopping at the lead.
        """
        if self._visible_length(article["extract"]) >= _SHORT_EXTRACT_CHARS:
            return
        extended = self._fetch_extended_extract(title)
        if extended and self._visible_length(extended) > self._visible_length(article["extract"]):
            article["extract"] = extended

    def _fetch_extended_extract(self, title: str) -> str | None:
        """Fetch a longer HTML extract spanning the whole article body.

        Unlike ``_fetch_summary``, this isn't limited to the lead section -
        the full article's parsed markup is requested (real headings,
        paragraphs, and lists, not flattened plain text) and then sanitized
        and trimmed to ``_EXTENDED_EXTRACT_CHARS`` on our end.
        """
        params: dict[str, str | int] = {
            "action": "query",
            "prop": "extracts",
            "titles": title,
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if raw := (page.get("extract") or "").strip():
                    return self._clean_and_trim_extract(raw)
        except Exception:
            logger.warning("Wikipedia extended extract fetch failed for %r", title)
        return None

    @staticmethod
    def _visible_length(html_fragment: str) -> int:
        """Return the rendered-text length of an HTML fragment, ignoring markup."""
        if not html_fragment:
            return 0
        return len(lxml_html.fromstring(f"<div>{html_fragment}</div>").text_content())

    @staticmethod
    def _clean_and_trim_extract(raw_html: str) -> str:
        """Sanitize untrusted article HTML, drop reference-style sections, and cap length.

        Truncation happens at block-element boundaries (never mid-tag or
        mid-sentence) so the result is always well-formed and never ends on a
        dangling heading.
        """
        safe_html = nh3.clean(raw_html, tags=_ALLOWED_TAGS, clean_content_tags=_CLEAN_CONTENT_TAGS, attributes={})
        root = lxml_html.fromstring(f"<div>{safe_html}</div>")

        kept: list[lxml_html.HtmlElement] = []
        total_len = 0
        for child in root:
            if child.tag in _HEADING_TAGS and child.text_content().strip().lower() in _STOP_HEADING_TITLES:
                break
            block_len = len(child.text_content())
            if kept and total_len + block_len > _EXTENDED_EXTRACT_CHARS:
                break
            kept.append(child)
            total_len += block_len

        while kept and kept[-1].tag in _HEADING_TAGS:
            kept.pop()

        return "".join(lxml_html.tostring(el, encoding="unicode") for el in kept).strip()

    def _geo_search(self, lat: float, lng: float) -> list[dict]:
        """Return up to _MAX_CANDIDATES article stubs near the coordinates."""
        params: dict[str, str | int] = {
            "action": "query",
            "list": "geosearch",
            "gscoord": f"{lat}|{lng}",
            "gsradius": _RADIUS_METERS,
            "gslimit": _MAX_CANDIDATES,
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json().get("query", {}).get("geosearch", [])
        except Exception:
            logger.exception("Wikipedia geo search failed for %s,%s", redact_coordinate(lat), redact_coordinate(lng))
            return []

    def _fetch_summary(self, title: str) -> dict | None:
        """Fetch the REST summary for a single article title."""
        url = _SUMMARY_URL.format(title=title.replace(" ", "_"))
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.warning("Wikipedia summary fetch failed for %r", title)
            return None

    @staticmethod
    def _address_matches(summary: dict, components: dict[str, str], name: str = "") -> bool:
        """
        Returns True if the candidate's title matches ``name``, or at least one
        address component appears in the article text.

        A title match on the place's own name is checked first since it is the
        strongest signal available - stronger than any address mention, which
        can also be true of unrelated articles about nearby places. We check
        the extract (first few paragraphs) for the city/locality as a fallback
        signal.  A street address match is stronger but optional.
        """
        title = (summary.get("title") or "").strip().lower()
        name = name.strip().lower()
        if name and title and (name in title or title in name):
            return True

        text = (summary.get("extract") or "").lower()
        if not text:
            return True  # no extract - accept the article, let the user judge

        locality = (components.get("locality") or "").lower()
        route = (components.get("route") or "").lower()
        street_number = (components.get("street_number") or "").strip()

        if locality and locality in text:
            return True
        if route and route in text:
            return True
        if street_number and street_number in text:
            return True

        # Fallback: accept articles with very short extracts (stub articles
        # often lack address mentions but are still correct).
        return len(text) < 200

    @staticmethod
    def _normalise(summary: dict) -> dict[str, Any]:
        """Shape the raw REST summary into our standard dict."""
        thumbnail = summary.get("thumbnail") or {}
        if raw_extract_html := summary.get("extract_html"):
            extract = WikipediaGateway._clean_and_trim_extract(raw_extract_html)
        elif raw_extract := summary.get("extract"):
            # No extract_html in this response (unexpected, but the REST API
            # doesn't guarantee it) - fall back to the plain-text extract,
            # escaped since it's rendered with the `safe` filter downstream.
            extract = f"<p>{escape(raw_extract)}</p>"
        else:
            extract = ""
        return {
            "title": summary.get("title", ""),
            "extract": extract,
            "url": summary.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "thumbnail": thumbnail.get("source", ""),
            "description": summary.get("description", ""),
            "page_id": summary.get("pageid"),
        }
