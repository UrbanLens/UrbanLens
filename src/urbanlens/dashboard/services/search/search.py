"""Search provider factory and pin search query helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError
from urbanlens.dashboard.services.security.redact import redact_text

logger = logging.getLogger(__name__)


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
    def search_web(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """
        Search the web.

        Args:
            query: The search string.
            max_results: Maximum number of results to request.

        Returns:
            Result dicts (``title``, ``link``, ``snippet``, ``date``, ``thumbnail``); empty when the provider rejected the query.

        Raises:
            ProviderUnavailableError: The provider could not be reached or every source behind it failed.
        """


def search_web(query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    """
    Search the web through the first available provider.
    There is no built-in provider: an install with none simply has no web search results, which degrades to the "Web Search" pin panel's existing empty state (see ``PinController._web_search_response``) rather than raising.

    Args:
        query: The search string.
        max_results: Maximum number of results to request.

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
        return provider.search_web(query, max_results=max_results)
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
