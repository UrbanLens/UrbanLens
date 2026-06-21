"""Brave Web Search API gateway."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)


class BraveSearchError(RuntimeError):
    """Raised when the Brave Search API cannot complete a request."""


def _mask_secret(value: str | None) -> str:
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...{value[-4:]}"


@dataclass(frozen=True, slots=True, kw_only=True)
class BraveSearchGateway(Gateway):
    """Gateway for the Brave Web Search API.

    Docs: https://api.search.brave.com/app/documentation/web-search/get-started
    Auth: X-Subscription-Token header.
    """

    api_key: str | None = None
    base_url: str = "https://api.search.brave.com/res/v1/web/search"

    def __post_init__(self) -> None:
        if self.api_key is None:
            object.__setattr__(self, "api_key", settings.brave_search_api_key)

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Perform a Brave web search and return normalised result dicts.

        Args:
            query: The search string.
            max_results: Number of results to request (1-20, Brave max).

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``.

        Raises:
            BraveSearchError: When the API key is missing or the request fails.
        """
        self._validate()
        params = {
            "q": query,
            "count": max(1, min(max_results, 20)),
        }
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        response = self.session.get(self.base_url, params=params, headers=headers, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning(
                "Brave Search request failed with status %s; key=%s",
                response.status_code,
                _mask_secret(self.api_key),
            )
            raise BraveSearchError(
                f"Brave Search request failed with status {response.status_code}",
            ) from exc
        return self._parse(response.json())

    def _validate(self) -> None:
        if not self.api_key:
            raise BraveSearchError("UL_BRAVE_SEARCH_API_KEY is not configured.")

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("web", {}).get("results", []):
            results.append(
                {
                    "title": item.get("title"),
                    "link": item.get("url"),
                    "snippet": item.get("description"),
                },
            )
        return results
