"""Wikimedia Commons service — searches for freely licensed media by name."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)

_API_URL = "https://commons.wikimedia.org/w/api.php"
_THUMB_WIDTH = 400
_MAX_RESULTS = 12
_USER_AGENT = "UrbanLens/1.0 (https://github.com/urbanlens/urbanlens; jess.a.mann@gmail.com) python-requests/2.x"


@dataclass(frozen=True, slots=True, kw_only=True)
class WikimediaGateway(Gateway):
    """
    Searches Wikimedia Commons for freely licensed images.

    Only call this when the pin has a meaningful name — coordinate-only names
    produce low-quality Commons results.
    """

    service_key: ClassVar[str] = "wikimedia"

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": _USER_AGENT})

    def search_images(self, query: str) -> list[dict[str, Any]]:
        """
        Search Commons for images matching *query* and return thumbnail info.

        Args:
            query: Human-readable location name used as the search term.

        Returns:
            List of dicts with keys ``title``, ``url``, ``thumb``,
            ``description_url``, ``mime``.  Empty list on failure or no results.
        """
        page_ids = self._search_files(query)
        if not page_ids:
            return []
        return self._fetch_image_info(page_ids)

    # ── private ────────────────────────────────────────────────────────────────

    def _search_files(self, query: str) -> list[str]:
        """Return up to _MAX_RESULTS file titles from a Commons full-text search."""
        params: dict[str, str | int] = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 6,  # File namespace
            "srlimit": _MAX_RESULTS,
            "srprop": "snippet",
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=10)
            resp.raise_for_status()
            hits = resp.json().get("query", {}).get("search", [])
            return [h["title"] for h in hits]
        except Exception:
            logger.exception("Wikimedia search failed for %r", query)
            return []

    def _fetch_image_info(self, titles: list[str]) -> list[dict[str, Any]]:
        """Batch-fetch image URLs and thumbnail URLs for the given file titles."""
        params: dict[str, str | int] = {
            "action": "query",
            "titles": "|".join(titles),
            "prop": "imageinfo",
            "iiprop": "url|mime|extmetadata",
            "iiurlwidth": _THUMB_WIDTH,
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=15)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {}).values()
        except Exception:
            logger.exception("Wikimedia imageinfo fetch failed")
            return []

        results = []
        for page in pages:
            if "imageinfo" not in page:
                continue
            info = page["imageinfo"][0]
            mime = info.get("mime", "")
            if not mime.startswith("image/"):
                continue  # skip audio/video/pdf files
            ext_meta = info.get("extmetadata", {})
            description = (
                ext_meta.get("ImageDescription", {}).get("value", "")
                or ext_meta.get("ObjectName", {}).get("value", "")
                or page.get("title", "").replace("File:", "")
            )
            results.append(
                {
                    "title": page.get("title", "").replace("File:", ""),
                    "url": info.get("url", ""),
                    "thumb": info.get("thumburl", ""),
                    "description_url": info.get("descriptionurl", ""),
                    "description": _strip_html(description),
                    "mime": mime,
                },
            )
        return results


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string without importing a full parser."""
    import re

    return re.sub(r"<[^>]+>", "", text).strip()
