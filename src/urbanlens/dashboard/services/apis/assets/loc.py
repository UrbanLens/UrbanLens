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
    # NOTE: quoting the name (quote_name=True) was tried and reverted -- LOC's
    # /search/ endpoint returned wildly unrelated results (e.g. out-of-state
    # newspaper archives) once the name was wrapped in quotes, likely because
    # its query parser doesn't handle a quoted phrase containing punctuation
    # (apostrophes, periods) the way a phrase-search operator normally would.
    # Street addresses are excluded for the same underlying reason: LOC's
    # relevance ranking appears to treat each word as an independent OR term
    # rather than requiring a phrase match, so a house number or generic
    # street-type word ("Road", "Street") coincidentally matches unrelated
    # historical records nationwide instead of narrowing results - a query
    # like "1265 Section Rd Cincinnati OH" returned newspaper archives from
    # Maryland and California. Searching on name + city/state only is both
    # more selective (no noise words) and a better fit for how LOC's
    # collections are actually catalogued (historical documents/photos rarely
    # carry modern street-address-level metadata anyway).
    include_address: ClassVar[bool] = False
    # A pin with no real landmark name (just its raw street address as a
    # fallback "name") produces a search with no genuine narrowing power for
    # LOC's word-independent relevance ranking - skip the provider entirely
    # for such a pin instead of guaranteeing noisy results.
    reject_address_derived_names: ClassVar[bool] = True

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
        # (connect, read) tuple, not a single scalar: a single timeout= only bounds
        # inactivity between reads, so a connection that trickles bytes slowly (as
        # seen in production - IncompleteRead after 30s+ of intermittent activity)
        # can still run far longer than intended. This runs inside the panel-fetch
        # Celery task (services/external_data.py); a search this slow is failing,
        # and failing fast lets the task's failure policy suppress and retry later.
        response = self.session.get(url, params=params, timeout=(5, 15))
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

    def _generate_media(self, search_term: str, address: str | None = None) -> Generator[MediaItem]:
        if not search_term:
            return
        for item in self.search(search_term):
            url = item.get("url") or ""
            if not url:
                continue
            # Many LOC records (books, HABS/HAER surveys, ...) have no preview
            # image at all - still yield them with an empty thumb_url rather
            # than dropping them, so they show up as a fallback tile in the
            # Media gallery instead of disappearing entirely.
            yield MediaItem(
                url=url,
                thumb_url=item.get("thumbnail") or "",
                caption=item.get("title") or "",
                source=self.display_name,
                page_url=url,
            )
