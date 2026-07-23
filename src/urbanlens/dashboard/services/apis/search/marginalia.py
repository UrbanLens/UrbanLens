"""Marginalia Search API gateway.

Marginalia (https://marginalia-search.com) is an independent, non-commercial
search engine that deliberately favours small, text-heavy, low-SEO sites over
the mainstream web - a good fit for surfacing niche forum threads, personal
blogs, and archives about a place that Google/Bing bury under listicles.

Marginalia publishes a shared ``public`` API key for light testing (rate
limited and shared across every consumer that hasn't requested their own
key), so this gateway works out of the box with no configuration; set
``UL_MARGINALIA_API_KEY`` to a dedicated key for production use.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.dashboard.services.redact import redact_secret
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: Marginalia's own shared testing key - rate limited and shared across every
#: consumer that hasn't requested a dedicated key. See
#: https://about.marginalia-search.com/article/api/
PUBLIC_TEST_API_KEY = "public"


class MarginaliaError(RuntimeError):
    """Raised when the Marginalia Search API cannot complete a request."""


@dataclass(slots=True, kw_only=True)
class MarginaliaGateway(Gateway):
    """Gateway for the Marginalia Search API.

    Docs: https://about.marginalia-search.com/article/api/
    Auth: ``API-Key`` header (defaults to the shared ``public`` testing key).
    """

    service_key: ClassVar[str] = "marginalia_search"
    paid_service: ClassVar[bool] = False

    api_key: str | None = None
    base_url: str = "https://api2.marginalia-search.com/search"

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if self.api_key is None:
            object.__setattr__(self, "api_key", settings.marginalia_api_key or PUBLIC_TEST_API_KEY)

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Perform a Marginalia search and return normalised result dicts.

        Args:
            query: The search string.
            max_results: Number of results to request (1-100).

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``.

        Raises:
            MarginaliaError: When the request fails (a 503 usually means the
                shared ``public`` key hit its rate limit - configure a
                dedicated key via UL_MARGINALIA_API_KEY).
        """
        params: dict[str, str | int] = {
            "query": query,
            "count": max(1, min(max_results, 100)),
        }
        headers = {"API-Key": self.api_key} if self.api_key else {}
        response = self.session.get(self.base_url, params=params, headers=headers, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning(
                "Marginalia Search request failed with status %s; key=%s",
                response.status_code,
                redact_secret(self.api_key),
            )
            raise MarginaliaError(f"Marginalia Search request failed with status {response.status_code}") from exc
        return self._parse(response.json())

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title"),
                    "link": item.get("url"),
                    "snippet": item.get("description"),
                    "date": None,
                    "thumbnail": None,
                },
            )
        return results
