"""Wikipedia service - finds and verifies articles for a pin's location."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_GEO_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
_RADIUS_METERS = 500
_MAX_CANDIDATES = 5
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"

# The REST summary endpoint (`_fetch_summary`) truncates well below the
# article's actual lead section - e.g. it returns ~550 characters for
# "Cincinnati" while the full lead section is ~2000. When a summary comes
# back shorter than this, it's worth a couple more requests to give the
# frontend enough text to fill whatever space its card ends up with (see
# the client-side clamp in dashboard/pages/location/index.html).
_SHORT_EXTRACT_CHARS = 400
# MediaWiki's TextExtracts prop hard-caps `exchars` at 1200 regardless of the
# value requested - used as a last-resort fallback to pull a bit of body text
# for genuine stub articles whose lead section alone is still short.
_EXCHARS_LIMIT = 1200


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
            logger.exception("Wikipedia nearby search failed for %s,%s", latitude, longitude)
            return []

        places = []
        for item in results:
            title = item.get("title", "")
            page_id = item.get("pageid")
            if not title or page_id is None:
                continue
            places.append({
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
            })
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
            name: The place's own name (e.g. pin/location name), when known.
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

    # ── private ────────────────────────────────────────────────────────────────

    def _fill_short_extract(self, article: dict[str, Any], title: str) -> None:
        """Mutate ``article["extract"]`` in place with more text when it's short.

        Tries progressively broader (and more expensive) sources: first the
        article's full lead section (uncapped, but naturally bounded - see
        ``_SHORT_EXTRACT_CHARS``), then a capped slice starting from the top
        of the whole article if the lead itself is still short (a genuine stub).
        Each step only runs if the previous one didn't produce enough text.
        """
        if len(article["extract"]) >= _SHORT_EXTRACT_CHARS:
            return
        if (intro := self._fetch_extract(title, intro_only=True)) and len(intro) > len(article["extract"]):
            article["extract"] = intro
        if len(article["extract"]) >= _SHORT_EXTRACT_CHARS:
            return
        if (extended := self._fetch_extract(title, intro_only=False)) and len(extended) > len(article["extract"]):
            article["extract"] = extended

    def _fetch_extract(self, title: str, *, intro_only: bool) -> str | None:
        """Fetch a plain-text extract via the action API's TextExtracts prop.

        Args:
            title: Article title.
            intro_only: When True, return the full lead section (see the
                module docstring on ``_SHORT_EXTRACT_CHARS`` for why this is
                already more than ``_fetch_summary`` provides). When False,
                return up to ``_EXCHARS_LIMIT`` characters starting from the
                top of the whole article, for stubs with a short lead too.
        """
        params: dict[str, str | int] = {
            "action": "query",
            "prop": "extracts",
            "titles": title,
            "explaintext": 1,
            "format": "json",
        }
        if intro_only:
            params["exintro"] = 1
        else:
            params["exchars"] = _EXCHARS_LIMIT
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                if extract := (page.get("extract") or "").strip():
                    return extract
        except Exception:
            logger.warning("Wikipedia extract fetch failed for %r (intro_only=%s)", title, intro_only)
        return None

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
            logger.exception("Wikipedia geo search failed for %s,%s", lat, lng)
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
        return {
            "title": summary.get("title", ""),
            "extract": summary.get("extract", ""),
            "url": summary.get("content_urls", {}).get("desktop", {}).get("page", ""),
            "thumbnail": thumbnail.get("source", ""),
            "description": summary.get("description", ""),
            "page_id": summary.get("pageid"),
        }
