"""Search provider factory and pin search query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError, redata_configured
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


def search_web(query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    """Search the web via REData's ``/search/web/`` provider fallback chain.

    REData already implements the same "try each provider in order, first to
    answer wins" chain this function used to run locally (SearXNG, Brave,
    Mojeek, Marginalia, Google Programmable Search, DuckDuckGo) - see
    ``../REData/docs/api-reference.md``, "GET /search/web/ - web search".
    There is no local fallback: an install with no REData configured simply
    has no web search results, which degrades to the "Web Search" pin
    panel's existing empty state (see ``PinController._web_search_response``)
    rather than raising.

    Args:
        query: The search string.
        max_results: Maximum number of results to request.

    Returns:
        Result dicts (``title``, ``link``, ``snippet``, ``date``,
        ``thumbnail``), or ``[]`` when REData is unconfigured or every
        provider it tried failed to answer.
    """
    if not redata_configured():
        return []
    from urbanlens.dashboard.services.apis.locations.redata_search_gateway import RedataSearchGateway

    try:
        return RedataSearchGateway().search_web(query, max_results=max_results)
    except LocationContextUnavailableError as exc:
        logger.warning("REData web search unavailable for %r: %s", query, exc)
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
            dt = datetime.strptime(raw[:19].rstrip("Z"), fmt.rstrip("%z"))  # noqa: DTZ007  # tzinfo=UTC is applied on the next line
            dt = dt.replace(tzinfo=UTC)
            return _format_relative_search_date(dt)
        except ValueError:
            continue
    return raw
