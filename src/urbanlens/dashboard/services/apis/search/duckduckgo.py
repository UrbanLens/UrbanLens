"""DuckDuckGo Instant Answer API gateway.

Free, keyless, no signup required. Note this is *not* a general web-search
SERP - DuckDuckGo does not offer a public API for full search results. The
Instant Answer API instead returns a topic abstract (often sourced from
Wikipedia) plus a handful of related-topic links, so coverage is limited to
queries that resolve to a recognised topic. Useful as a free supplementary
source, not a primary search provider for arbitrary queries.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway

logger = logging.getLogger(__name__)


class DuckDuckGoError(RuntimeError):
    """Raised when the DuckDuckGo Instant Answer API cannot complete a request."""


@dataclass(slots=True, kw_only=True)
class DuckDuckGoGateway(Gateway):
    """Gateway for the DuckDuckGo Instant Answer API.

    Docs: https://duckduckgo.com/duckduckgo-help-pages/results/duckduckgo-api/
    Auth: none.
    """

    service_key: ClassVar[str] = "duckduckgo"
    paid_service: ClassVar[bool] = False

    base_url: str = "https://api.duckduckgo.com/"

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Fetch a DuckDuckGo instant answer and its related topics.

        Args:
            query: The search string.
            max_results: Maximum number of results to return.

        Returns:
            List of dicts with keys ``title``, ``link``, ``snippet``. May be
            shorter than ``max_results`` (often empty) since this endpoint
            only returns instant-answer content, not a full result set.

        Raises:
            DuckDuckGoError: When the request fails.
        """
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        response = self.session.get(self.base_url, params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            logger.warning("DuckDuckGo Instant Answer request failed with status %s", response.status_code)
            raise DuckDuckGoError(f"DuckDuckGo request failed with status {response.status_code}") from exc
        return self._parse(response.json())[:max_results]

    def _parse(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []

        abstract_text = data.get("AbstractText")
        abstract_url = data.get("AbstractURL")
        if abstract_text and abstract_url:
            results.append(
                {
                    "title": data.get("Heading") or abstract_text[:80],
                    "link": abstract_url,
                    "snippet": abstract_text,
                    "date": None,
                    "thumbnail": None,
                },
            )

        for topic in data.get("RelatedTopics", []):
            # Disambiguation groups nest their own Topics list under "Name"; flatten one level.
            for entry in topic.get("Topics", [topic]):
                text = entry.get("Text")
                first_url = entry.get("FirstURL")
                if not text or not first_url:
                    continue
                title, _, snippet = text.partition(" - ")
                results.append(
                    {
                        "title": title or text,
                        "link": first_url,
                        "snippet": snippet or text,
                        "date": None,
                        "thumbnail": (entry.get("Icon") or {}).get("URL") or None,
                    },
                )

        return results
