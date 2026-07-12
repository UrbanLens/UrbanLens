"""Google Custom Search JSON API, image search mode.

Shares credentials and rate-limit accounting with ``GoogleCustomSearchGateway``
(the ``google_search`` service key) - Google's Custom Search JSON API bills
and quotas image searches identically to text searches against the same CSE,
so this is not a separate service to a user's quota, just a different
``searchType`` on the same endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from requests import HTTPError

from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchError, GoogleCustomSearchGateway

logger = logging.getLogger(__name__)


@dataclass(slots=True, kw_only=True)
class GoogleImageSearchGateway(GoogleCustomSearchGateway):
    """Image-search variant of the Google Custom Search JSON API gateway."""

    def search_images(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Search Google Images for ``query`` (an address - never a pin/wiki name).

        Args:
            query: The search text, e.g. a pin's street address.
            max_results: Maximum number of images to return (Google caps a
                single request at 10; this endpoint never paginates further).

        Returns:
            Dicts with keys ``title``, ``link`` (full image URL),
            ``thumbnail``, and ``context_link`` (the page the image was
            found on). Empty when the query is blank or nothing was found.
        """
        if not query:
            return []
        self.validate_configuration()
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "searchType": "image",
            "num": max(1, min(max_results, 10)),
        }
        response = self.session.get(self.base_url, params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            detail = self.extract_error_detail(response)
            logger.warning("Google Image Search request failed with status %s: %s", response.status_code, detail)
            raise GoogleCustomSearchError(f"Google Image Search request failed with status {response.status_code}: {detail}") from exc

        data = response.json()
        results: list[dict[str, Any]] = []
        for item in data.get("items", []):
            link = item.get("link")
            if not link:
                continue
            image_meta = item.get("image") or {}
            results.append(
                {
                    "title": item.get("title") or "",
                    "link": link,
                    "thumbnail": image_meta.get("thumbnailLink") or link,
                    "context_link": image_meta.get("contextLink") or link,
                },
            )
        return results
