from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.services.apis.assets.base import MediaItem, MediaProvider

if TYPE_CHECKING:
    from collections.abc import Generator


@dataclass(slots=True, kw_only=True)
class LOCJsonGateway(MediaProvider):
    """Gateway for the Library of Congress JSON API.

    Docs: https://www.loc.gov/apis/json-and-yaml-apis/
    Free tier; no API key required.
    """

    service_key: ClassVar[str] = "library_of_congress"
    display_name: ClassVar[str] = "Library of Congress"
    paid_service: ClassVar[bool] = False
    usa_only: ClassVar[bool] = True
    search_with_country: ClassVar[bool] = False

    base_url: str = "https://www.loc.gov"

    def search(self, query: str, *, count: int = 25) -> list[dict[str, Any]]:
        """Search LOC collections and return normalised result dicts.

        Args:
            query: Full-text search string.
            count: Maximum number of results to request.

        Returns:
            List of dicts with keys ``title``, ``url``, ``date``,
            ``thumbnail``, ``description``, ``subject``.
        """
        url = f"{self.base_url}/search/"
        params: dict[str, str | int] = {
            "q": query,
            "fo": "json",
            "at": "results,pagination",
            "c": count,
        }
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return self._parse(response.json())

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            image_data = item.get("image") or {}
            thumbnail = image_data.get("url") or image_data.get("thumb") or None
            description_parts = item.get("description") or []
            description = " ".join(description_parts) if isinstance(description_parts, list) else str(description_parts)
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url") or item.get("id", ""),
                    "date": item.get("date", ""),
                    "thumbnail": thumbnail,
                    "description": description,
                    "subject": item.get("subject") or [],
                },
            )
        return results

    def _generate_media(self, search_term: str) -> Generator[MediaItem]:
        if not search_term:
            return
        for item in self.search(search_term):
            thumbnail = item.get("thumbnail")
            if not thumbnail:
                continue
            yield MediaItem(
                url=item.get("url") or thumbnail,
                thumb_url=thumbnail,
                caption=item.get("title") or "",
                source=self.display_name,
                page_url=item.get("url") or "",
            )
