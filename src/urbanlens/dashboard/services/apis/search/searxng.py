"""SearXNG metasearch gateway.

SearXNG (https://docs.searxng.org/) is an open-source, self-hostable
metasearch engine that aggregates results from many upstream engines
(Google, Bing, DuckDuckGo, Brave, and dozens more) behind one privacy-respecting
API. There is no central SearXNG API key - each instance is independent, so
this gateway talks to whichever instance the admin configures.

Most public instances disable the JSON output format to discourage scraping,
so this integration is intended for a self-hosted instance (trivial to run
via the official Docker image) or a instance the admin has explicit
permission to query programmatically. No default base URL is supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class SearxngError(RuntimeError):
    """Raised when a SearXNG instance cannot complete a search request."""


@dataclass(slots=True, kw_only=True)
class SearxngGateway(Gateway):
    """Gateway for a self-hosted or trusted SearXNG metasearch instance.

    Docs: https://docs.searxng.org/dev/search_api.html
    Auth: none - configure ``base_url`` to point at an instance with JSON
    output enabled (``search.formats: [html, json]`` in ``settings.yml``).
    """

    service_key: ClassVar[str] = "searxng"
    paid_service: ClassVar[bool] = False

    base_url: str | None = None

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if self.base_url is None:
            object.__setattr__(self, "base_url", settings.searxng_base_url)

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Perform a SearXNG search and return normalised result dicts.

        Args:
            query: The search string.
            max_results: Maximum number of results to return.

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``.

        Raises:
            SearxngError: When no instance is configured or the request fails.
        """
        self._validate()
        params = {"q": query, "format": "json"}
        response = self.session.get(f"{self.base_url}/search", params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning("SearXNG request to %s failed with status %s", self.base_url, response.status_code)
            raise SearxngError(f"SearXNG request failed with status {response.status_code}") from exc
        return self._parse(response.json())[:max_results]

    def _validate(self) -> None:
        if not self.base_url:
            raise SearxngError("UL_SEARXNG_BASE_URL is not configured. Point it at a self-hosted or trusted SearXNG instance with JSON output enabled.")

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title"),
                    "link": item.get("url"),
                    "snippet": item.get("content"),
                    "date": item.get("publishedDate"),
                    "thumbnail": item.get("img_src"),
                },
            )
        return results
