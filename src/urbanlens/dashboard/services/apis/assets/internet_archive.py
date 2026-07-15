"""Internet Archive gateway - free, keyless, open full-text/media search.

https://archive.org/advancedsearch.php - distinct from this project's
existing Wayback Machine integration (URL snapshots): this searches every
item Internet Archive hosts (books, historical photos, newspapers, audio,
video) by keyword, matching against titles/descriptions/full text.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://archive.org/advancedsearch.php"
_THUMBNAIL_URL = "https://archive.org/services/img/{identifier}"
_DETAILS_URL = "https://archive.org/details/{identifier}"


def _first_str(value: Any) -> str:
    """Archive.org fields are inconsistently a bare string or a list of strings."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""

#: Fields requested from advancedsearch.php - keep in sync with ``_parse``.
_FIELDS = ("identifier", "title", "description", "date", "mediatype", "creator")

#: Restrict results to media types with a displayable preview image - excludes
#: books/audio/software/data noise that isn't useful in a photo gallery.
_MEDIA_TYPE_FILTER = "mediatype:(image OR movies)"


@dataclass(slots=True, kw_only=True)
class InternetArchiveGateway(MediaProvider):
    """Gateway for the Internet Archive's advancedsearch.php JSON API.

    Free, keyless, open-source project (archive.org). No rate limit is
    enforced by the API, but this project still tracks it like any other
    external call for admin visibility/throttling.
    """

    service_key: ClassVar[str] = "internet_archive"
    display_name: ClassVar[str] = "Internet Archive"
    paid_service: ClassVar[bool] = False

    def search(self, query: str, *, rows: int = 20) -> list[dict[str, Any]]:
        """Full-text/metadata search across every item Internet Archive hosts.

        Args:
            query: Free-text search query.
            rows: Maximum number of results to request.

        Returns:
            List of normalized dicts with keys ``identifier``, ``title``,
            ``description``, ``date``, ``mediatype``, ``creator``. Restricted
            to media types with a displayable preview (photos/film).
        """
        if not query:
            return []
        params: dict[str, Any] = {"q": f"{query} AND {_MEDIA_TYPE_FILTER}", "fl[]": list(_FIELDS), "rows": rows, "output": "json"}
        response = self.session.get(_SEARCH_URL, params=params, timeout=(5, 15))
        response.raise_for_status()
        docs = (response.json().get("response") or {}).get("docs") or []
        return [
            {
                "identifier": doc.get("identifier") or "",
                "title": doc.get("title") or "",
                "description": doc.get("description") or "",
                "date": (doc.get("date") or "")[:10],
                "mediatype": doc.get("mediatype") or "",
                "creator": _first_str(doc.get("creator")),
            }
            for doc in docs
            if doc.get("identifier")
        ]

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        """Yield items whose mediatype has a displayable image (photos, texts with covers)."""
        for item in self.search(search_term):
            identifier = item["identifier"]
            description = item.get("description") or ""
            if isinstance(description, list):
                description = " ".join(str(part) for part in description)
            yield MediaItem(
                url=_DETAILS_URL.format(identifier=identifier),
                thumb_url=_THUMBNAIL_URL.format(identifier=identifier),
                caption=item.get("title") or item.get("creator") or description[:120] or identifier,
                source=self.display_name,
                page_url=_DETAILS_URL.format(identifier=identifier),
            )
