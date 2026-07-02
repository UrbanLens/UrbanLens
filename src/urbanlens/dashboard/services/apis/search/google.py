from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from requests import HTTPError

from urbanlens.dashboard.services.gateway import Gateway
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    import requests

logger = logging.getLogger(__name__)


class GoogleCustomSearchError(RuntimeError):
    """Raised when Google Custom Search cannot complete a request safely."""


def _mask_secret(value: str | None) -> str:
    """Return a log-safe representation of a Google API key or CSE id."""
    if not value:
        return "<missing>"
    if len(value) <= 8:
        return "<redacted>"
    return f"{value[:4]}...{value[-4:]}"


@dataclass(slots=True, kw_only=True)
class GoogleCustomSearchGateway(Gateway):
    """
    Gateway for the Google Custom Search API.
    """

    service_key: ClassVar[str] = "google_search"
    paid_service: ClassVar[bool] = True

    api_key: str | None = settings.google_unrestricted_api_key
    cx: str | None = settings.google_search_tenant
    base_url: str = "https://customsearch.googleapis.com/customsearch/v1"

    def search(
        self,
        terms: str | list[str | list[str | None] | None],
        *,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        """
        Perform a search using the Google Custom Search JSON API.
        """
        self.validate_configuration()
        query = self.build_query(terms)

        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            # Google Custom Search JSON API accepts 1-10 results per request.
            "num": max(1, min(max_results, 10)),
        }
        response = self.session.get(self.base_url, params=params, timeout=60)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            detail = self.extract_error_detail(response)
            logger.warning(
                "Google Custom Search request failed with status %s; key=%s cx=%s reason=%s",
                response.status_code,
                _mask_secret(self.api_key),
                _mask_secret(self.cx),
                detail,
            )
            raise GoogleCustomSearchError(
                f"Google Custom Search request failed with status {response.status_code}: {detail}",
            ) from exc
        return self.parse_response(response)

    def validate_configuration(self) -> None:
        """Fail before issuing a request when required credentials are missing."""
        if not self.api_key:
            raise GoogleCustomSearchError("UL_GOOGLE_SEARCH_API_KEY is not configured.")
        if not self.cx:
            raise GoogleCustomSearchError("UL_GOOGLE_SEARCH_TENANT/UL_GOOGLE_SEARCH_CX is not configured.")

    def extract_error_detail(self, response: requests.Response) -> str:
        """Extract a concise Google API error without including the API key URL."""
        try:
            error = response.json().get("error", {})
        except ValueError:
            return response.text[:300] or "No response body"

        message = error.get("message") or "No error message returned"
        reasons = []
        for item in error.get("errors", []):
            reason = item.get("reason")
            if reason:
                reasons.append(reason)
        if reasons:
            return f"{message} ({', '.join(reasons)})"
        return message

    def parse_response(self, response: requests.Response) -> list[dict[str, Any]]:
        """
        Extract search results from the API response.
        """
        data = response.json()

        results: list[dict[str, Any]] = []
        for item in data.get("items", []):
            pagemap = item.get("pagemap", {})
            metatags = (pagemap.get("metatags") or [{}])[0]
            date_published = (
                metatags.get("article:published_time")
                or metatags.get("article:modified_time")
                or metatags.get("og:updated_time")
                or (pagemap.get("newsarticle") or [{}])[0].get("datepublished")
                or (pagemap.get("article") or [{}])[0].get("datepublished")
            )
            thumbnail = metatags.get("og:image") or (pagemap.get("cse_thumbnail") or [{}])[0].get("src")
            result = {
                "title": item.get("title"),
                "link": item.get("link"),
                "snippet": item.get("snippet"),
                "date": date_published,
                "thumbnail": thumbnail,
            }
            results.append(result)
        return results

    def preprocess_query_terms(self, terms: Sequence[str | None]) -> list[str]:
        """
        Build a query string from a list of search terms using OR.

        Args:
            terms (list[str]): A list of search terms.

        """
        # Join all terms with "OR", and wrap in quotes. Do not wrap terms that already have quotes, or begin with parenthesis
        query_terms = []

        for term in terms:
            if not term:
                continue

            normalized = term.strip()
            if normalized.startswith(('"', "(")):
                query_terms.append(normalized)
            else:
                # sanitize existing quotes in term
                value = normalized.replace('"', '\\"')
                query_terms.append(f'"{value}"')

        return query_terms

    def build_query_or(self, terms: Sequence[str | None]) -> str:
        """
        Build a query string from a list of search terms using OR.

        Args:
            terms (list[str]): A list of search terms.

        """
        query_terms = self.preprocess_query_terms(terms)

        query = " OR ".join(query_terms)
        if len(query_terms) > 1:
            return f"({query})"
        return query

    def build_query_and(self, terms: Sequence[str | None]) -> str:
        """
        Build a query string from a list of search terms using AND.

        Args:
            terms (list[str]): A list of search terms.

        """
        query_terms = self.preprocess_query_terms(terms)

        query = " AND ".join(query_terms)
        if len(query_terms) > 1:
            return f"({query})"
        return query

    def build_query(self, terms: str | list[str | list[str | None] | None]) -> str:
        """
        Accepts input like:
        [
            'or_term1',
            'or_term2',
            [
                'and_term3',
                'and_term4'
            ],
            'or_term5',
        ]
        Defaults to OR when combining lists
        """
        if isinstance(terms, str):
            return terms

        query_terms = []

        for term in terms:
            if isinstance(term, list):
                built_term = self.build_query_and(term)
                if built_term:
                    query_terms.append(built_term)
            elif term is not None:
                query_terms.append(term)

        return self.build_query_or(query_terms)
