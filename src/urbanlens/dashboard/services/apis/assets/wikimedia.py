"""Wikimedia Commons service - searches for freely licensed media by name."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import PAGED_DOCUMENT_CONTENT_TYPES, MediaItem, MediaProvider
from urbanlens.dashboard.services.core.gateway import Gateway, is_source_outage
from urbanlens.dashboard.services.core.user_agent import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_API_URL = "https://commons.wikimedia.org/w/api.php"
_THUMB_WIDTH = 400
_MAX_RESULTS = 60
# The MediaWiki API caps the `titles` parameter at 50 values per request for unauthenticated
# (non-bot) requests -- exceeding it doesn't truncate, it fails the whole request with a
# "toomanyvalues" error, silently dropping every result. _MAX_RESULTS (60) is above that limit, so
# imageinfo lookups must be chunked.
_TITLES_BATCH_SIZE = 50
_EXTMETADATA = "ImageDescription|ObjectName|Categories|GPSLatitude|GPSLongitude"
_FILE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{2,4}$")


@dataclass(slots=True, kw_only=True)
class WikimediaGateway(MediaProvider):
    """Searches Wikimedia Commons for freely licensed images.
    Only call this when the pin has a meaningful name - coordinate-only names produce low-quality Commons results."""

    service_key: ClassVar[str] = "wikimedia"
    display_name: ClassVar[str] = "Wikimedia Commons"
    paid_service: ClassVar[bool] = False
    multi_query: ClassVar[bool] = True
    # Commons' full-text search over individual file description pages appears to do (near-)strict
    # AND matching across query tokens - every extra qualifying term shrinks the candidate set, and
    # a country name rarely appears in a single file's own title/description text.
    # Confirmed by hand: "<name> <city> <state>" returns hits; the same query plus "United States"
    search_with_country: ClassVar[bool] = False
    # Loose name words match unrelated files whose description mentions each word somewhere.
    quote_name: ClassVar[bool] = True

    base_url: str = _API_URL

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        self.session.headers.update({"User-Agent": USER_AGENT})

    def search_images(self, query: str) -> list[dict[str, Any]]:
        """Search Commons for images matching *query* and return thumbnail info.

        Args:
            query: Human-readable wiki/place name used as the search term.

        Returns:
            List of dicts with keys ``title``, ``name``, ``url``, ``thumb``, ``description_url``, ``description``, ``caption``, ``categories``, ``latitude``, ``longitude``, ``mime``.
        """
        page_ids = self._search_files(query)
        if not page_ids:
            return []
        return self._fetch_image_info(page_ids)

    # -- private ----------------------------------------------------------------

    def _search_files(self, query: str) -> list[str]:
        """Return up to _MAX_RESULTS file titles from a Commons full-text search.

        Raises:
            Exception: Commons could not be asked (see ``is_source_outage``).
        """
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
        except Exception as exc:
            if is_source_outage(exc):
                raise
            logger.exception("Wikimedia search failed for %r", query)
            return []

    def _fetch_image_info(self, titles: list[str]) -> list[dict[str, Any]]:
        """Fetch image URLs and thumbnail URLs for the given file titles."""
        results: list[dict[str, Any]] = []
        for i in range(0, len(titles), _TITLES_BATCH_SIZE):
            results.extend(self._fetch_image_info_batch(titles[i : i + _TITLES_BATCH_SIZE]))
        return results

    def _fetch_image_info_batch(self, titles: list[str]) -> list[dict[str, Any]]:
        """Fetch image info and coordinates for a single batch of at most _TITLES_BATCH_SIZE titles.

        Images and paged documents (PDF, DjVu) are kept; sound and video are not.

        Raises:
            Exception: Commons could not be asked (see ``is_source_outage``).
        """
        params: dict[str, str | int] = {
            "action": "query",
            "titles": "|".join(titles),
            "prop": "imageinfo|coordinates",
            "iiprop": "url|mime|extmetadata",
            "iiextmetadatafilter": _EXTMETADATA,
            "iiurlwidth": _THUMB_WIDTH,
            # Commons returns coordinates for 10 pages unless asked for more.
            "colimit": "max",
            "format": "json",
        }
        try:
            resp = self.session.get(self.base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                logger.warning("Wikimedia imageinfo fetch returned an error: %r", data["error"])
                return []
            pages = data.get("query", {}).get("pages", {}).values()
        except Exception as exc:
            if is_source_outage(exc):
                raise
            logger.exception("Wikimedia imageinfo fetch failed")
            return []

        results = []
        for page in pages:
            if "imageinfo" not in page:
                continue
            info = page["imageinfo"][0]
            mime = info.get("mime", "")
            if not (mime.startswith("image/") or mime in PAGED_DOCUMENT_CONTENT_TYPES):
                continue
            ext_meta = info.get("extmetadata", {})
            title = page.get("title", "").removeprefix("File:")
            name = _strip_html(_meta(ext_meta, "ObjectName")) or _FILE_EXTENSION.sub("", title)
            description = _strip_html(_meta(ext_meta, "ImageDescription"))
            latitude, longitude = _location(page, ext_meta)
            results.append(
                {
                    "title": title,
                    "name": name,
                    "url": info.get("url", ""),
                    "thumb": info.get("thumburl", ""),
                    "description_url": info.get("descriptionurl", ""),
                    "description": description,
                    "caption": description or name or title,
                    "categories": _meta(ext_meta, "Categories"),
                    "latitude": latitude,
                    "longitude": longitude,
                    "mime": mime,
                },
            )
        return results

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        if not search_term:
            return
        for img in self.search_images(search_term):
            url = img.get("url") or img.get("thumb")
            if not url:
                continue
            yield MediaItem(
                url=img.get("url") or img.get("thumb") or "",
                thumb_url=img.get("thumb") or img.get("url") or "",
                caption=img.get("caption") or "",
                source=self.display_name,
                page_url=img.get("description_url") or "",
                content_type=img.get("mime") or "",
                title=img.get("name") or "",
                description=img.get("description") or "",
                keywords=img.get("categories") or "",
                latitude=img.get("latitude"),
                longitude=img.get("longitude"),
            )


def _meta(ext_meta: dict[str, Any], key: str) -> str:
    """One extmetadata field's value, as text."""
    value = (ext_meta.get(key) or {}).get("value")
    return str(value) if value is not None else ""


def _number(value: object) -> float | None:
    try:
        return float(str(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _location(page: dict[str, Any], ext_meta: dict[str, Any]) -> tuple[float | None, float | None]:
    """Where Commons places a file: its primary coordinates, else the GPS position in its metadata."""
    for coordinates in page.get("coordinates") or []:
        latitude, longitude = _number(coordinates.get("lat")), _number(coordinates.get("lon"))
        if latitude is not None and longitude is not None:
            return latitude, longitude
    latitude, longitude = _number(_meta(ext_meta, "GPSLatitude")), _number(_meta(ext_meta, "GPSLongitude"))
    if latitude is None or longitude is None:
        return None, None
    return latitude, longitude


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string without importing a full parser."""
    from urbanlens.dashboard.services.import_formats.html_description import strip_html

    return strip_html(text)
