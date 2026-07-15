"""Mojeek Search API gateway.

Mojeek (https://www.mojeek.com) runs its own independent web index rather
than reselling Google/Bing results, which makes it a useful second opinion
alongside Brave/Google - it surfaces smaller or less SEO-optimised sites that
larger engines deprioritise. The Search API has a free tier for low-volume,
non-commercial use.
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


class MojeekError(RuntimeError):
    """Raised when the Mojeek Search API cannot complete a request."""


@dataclass(slots=True, kw_only=True)
class MojeekGateway(Gateway):
    """Gateway for the Mojeek Search API.

    Docs: https://www.mojeek.com/support/api/search/
    Auth: ``api_key`` query parameter.
    """

    service_key: ClassVar[str] = "mojeek_search"
    paid_service: ClassVar[bool] = True

    api_key: str | None = None
    base_url: str = "https://api.mojeek.com/search"

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if self.api_key is None:
            object.__setattr__(self, "api_key", settings.mojeek_api_key)

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Perform a Mojeek web search and return normalised result dicts.

        Args:
            query: The search string.
            max_results: Number of results to request (Mojeek caps at 100 via ``t``).

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``.

        Raises:
            MojeekError: When the API key is missing or the request fails.
        """
        self._validate()
        params: dict[str, str | int] = {
            "q": query,
            "api_key": self.api_key,  # type: ignore[dict-item]
            "fmt": "json",
            "t": max(1, min(max_results, 100)),
        }
        response = self.session.get(self.base_url, params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning(
                "Mojeek Search request failed with status %s; key=%s",
                response.status_code,
                redact_secret(self.api_key),
            )
            raise MojeekError(f"Mojeek Search request failed with status {response.status_code}") from exc
        return self._parse(response.json())

    def _validate(self) -> None:
        if not self.api_key:
            raise MojeekError("UL_MOJEEK_API_KEY is not configured.")

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for item in data.get("response", {}).get("results", []):
            image = item.get("image") or {}
            results.append(
                {
                    "title": item.get("title"),
                    "link": item.get("url"),
                    "snippet": item.get("desc"),
                    "date": item.get("date"),
                    "thumbnail": image.get("url"),
                },
            )
        return results
