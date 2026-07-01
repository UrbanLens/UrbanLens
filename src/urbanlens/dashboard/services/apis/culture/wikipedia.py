"""Wikipedia service — finds and verifies articles for a pin's location."""

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


@dataclass(frozen=True, slots=True, kw_only=True)
class WikipediaGateway(Gateway):
    """
    Fetches Wikipedia article summaries for a geographic location.

    The address-verification step ensures we return an article that is actually
    about the queried address rather than a nearby unrelated landmark.
    """

    service_key: ClassVar[str] = "wikipedia"

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
    ) -> dict[str, Any] | None:
        """
        Find a Wikipedia article near the coordinates that mentions the address.

        Args:
            latitude: WGS-84 latitude of the location.
            longitude: WGS-84 longitude of the location.
            address_components: Dict with optional keys 'locality', 'route',
                'street_number', 'administrative_area_level_1'.

        Returns:
            A dict with keys ``title``, ``extract``, ``url``, ``thumbnail``,
            ``description``, ``page_id`` — or None if no matching article found.
        """
        candidates = self._geo_search(latitude, longitude)
        for candidate in candidates:
            summary = self._fetch_summary(candidate["title"])
            if summary and self._address_matches(summary, address_components):
                return self._normalise(summary)
        return None

    # ── private ────────────────────────────────────────────────────────────────

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
    def _address_matches(summary: dict, components: dict[str, str]) -> bool:
        """
        Returns True if at least one address component appears in the article text.

        We check the extract (first few paragraphs) for the city/locality — the
        most reliable signal.  A street address match is stronger but optional.
        """
        text = (summary.get("extract") or "").lower()
        if not text:
            return True  # no extract — accept the article, let the user judge

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
