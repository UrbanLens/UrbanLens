"""Search provider factory and pin search query helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.security.redact import redact_text

logger = logging.getLogger(__name__)

#: Domain separation for :func:`search_audience`, so the digest is useless against any other use of ``SECRET_KEY``.
_AUDIENCE_CONTEXT = b"urbanlens.web-search.audience:"


def search_audience(user_id: int) -> str:
    """
    A stable opaque label for one user, safe to send to the search provider.

    Args:
        user_id: The user's primary key.

    Returns:
        An HMAC of the id under ``SECRET_KEY``; the id itself is never recoverable from it.
    """
    from django.conf import settings

    return hmac.new(settings.SECRET_KEY.encode(), _AUDIENCE_CONTEXT + str(user_id).encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class WebSearchScope:
    """
    Who may read what a search finds, and which place it is about, as the search provider is told.

    Attributes:
        audience: Set for a private search: an opaque per-user label from :func:`search_audience`. Empty for a shared one.
        entity: ``<kind>:<value>`` of the place a shared search is about.
        entity_name: The place's name, so a rephrased query reuses the entity's answers.
        min_results: Fewest results to keep gathering before the provider stops.
    """

    audience: str = ""
    entity: str = ""
    entity_name: str = ""
    min_results: int = 1

    @property
    def private(self) -> bool:
        """Whether the results are kept for ``audience`` alone."""
        return bool(self.audience)

    def params(self) -> dict[str, str | int]:
        """The query parameters ``GET /search/web/`` takes for this scope."""
        params: dict[str, str | int] = {"scope": "private" if self.private else "shared"}
        if self.private:
            params["audience"] = self.audience
            return params
        if self.entity:
            params["entity"] = self.entity
            if self.entity_name:
                params["entity_name"] = self.entity_name
        if self.min_results > 1:
            params["min_results"] = self.min_results
        return params


class WebSearchProvider(ABC):
    """
    One source of web search results.

    Attributes:
        key: Short identifier, for logs.
    """

    key: ClassVar[str]

    @abstractmethod
    def available(self) -> bool:
        """Whether this provider is configured to answer."""

    @abstractmethod
    def search_web(self, query: str, *, max_results: int = 10, scope: WebSearchScope | None = None) -> list[dict[str, Any]]:
        """
        Search the web.

        Args:
            query: The search string.
            max_results: Maximum number of results to request.
            scope: Who may read the results, for a provider that keeps them apart; None for an ordinary shared search.

        Returns:
            Result dicts (``title``, ``link``, ``snippet``, ``date``, ``thumbnail``); empty when the provider rejected the query.

        Raises:
            ProviderUnavailableError: The provider could not be reached or every source behind it failed.
        """


def search_web(query: str, *, max_results: int = 10, scope: WebSearchScope | None = None) -> list[dict[str, Any]]:
    """
    Search the web through the first available provider.
    There is no built-in provider: an install with none simply has no web search results, which degrades to the "Web Search" pin panel's existing empty state (see ``PinController._web_search_response``) rather than raising.

    Args:
        query: The search string.
        max_results: Maximum number of results to request.
        scope: See :meth:`WebSearchProvider.search_web`.

    Returns:
        Result dicts (``title``, ``link``, ``snippet``, ``date``, ``thumbnail``), or ``[]`` when no provider is available or it rejected the query.

    Raises:
        ProviderUnavailableError: The provider could not be reached, or every source it tried failed to answer; nothing was learned, so a caller must not cache it as "no results".
    """
    from urbanlens.dashboard.plugins.points import WEB_SEARCH_PROVIDERS

    provider = next((candidate for candidate in WEB_SEARCH_PROVIDERS if candidate.available()), None)
    if provider is None:
        return []
    try:
        if scope is None:
            return provider.search_web(query, max_results=max_results)
        return provider.search_web(query, max_results=max_results, scope=scope)
    except ProviderUnavailableError as exc:
        if exc.is_outage:
            raise
        logger.warning("%s web search rejected %s: %s (reason=%s)", provider.key, redact_text(query), type(exc).__name__, exc.reason)
        return []


def _format_relative_search_date(dt: datetime) -> str:
    """Return a short relative display label for a parsed search-result date."""
    now = datetime.now(tz=UTC)
    delta = now - dt
    if delta.days < 1:
        hours = delta.seconds // 3600
        return f"{hours}h ago" if hours else "Just now"
    if delta.days < 7:
        return f"{delta.days}d ago"
    if delta.days < 365:
        return dt.strftime("%b %-d")
    return dt.strftime("%b %-d, %Y")


def format_search_date(raw: str | None) -> str:
    """Convert an ISO date string to a short display label (e.g. '2d ago', 'Jan 5')."""
    if not raw:
        return ""

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:19].rstrip("Z"), fmt.rstrip("%z"))  # noqa: DTZ007  # tzinfo=UTC is applied...
            dt = dt.replace(tzinfo=UTC)
            return _format_relative_search_date(dt)
        except ValueError:
            continue
    return raw
