"""Search provider factory and pin search query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from django.db import DatabaseError

from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


@runtime_checkable
class SearchGateway(Protocol):
    """Minimal interface shared by all search gateway implementations."""

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]: ...


def get_search_gateway() -> SearchGateway:
    """Return the search gateway selected in SiteSettings.

    Defaults to Brave if the database row does not exist yet.

    Returns:
        A gateway instance whose ``search(query)`` method returns a list of
        ``{"title": ..., "link": ..., "snippet": ...}`` dicts.
    """
    from urbanlens.dashboard.models.site_settings import SearchProviderChoice, SiteSettings

    try:
        provider = SiteSettings.get_current().search_provider
    except (ImportError, DatabaseError, Exception):
        provider = SearchProviderChoice.BRAVE

    if provider == SearchProviderChoice.GOOGLE:
        from urbanlens.dashboard.services.apis.search.google import GoogleCustomSearchGateway

        return GoogleCustomSearchGateway()

    if provider == SearchProviderChoice.SEARXNG:
        from urbanlens.dashboard.services.apis.search.searxng import SearxngGateway

        return SearxngGateway()

    if provider == SearchProviderChoice.DUCKDUCKGO:
        from urbanlens.dashboard.services.apis.search.duckduckgo import DuckDuckGoGateway

        return DuckDuckGoGateway()

    if provider == SearchProviderChoice.MOJEEK:
        from urbanlens.dashboard.services.apis.search.mojeek import MojeekGateway

        return MojeekGateway()

    if provider == SearchProviderChoice.MARGINALIA:
        from urbanlens.dashboard.services.apis.search.marginalia import MarginaliaGateway

        return MarginaliaGateway()

    from urbanlens.dashboard.services.apis.search.brave.search import BraveSearchGateway

    return BraveSearchGateway()


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
            dt = datetime.strptime(raw[:19].rstrip("Z"), fmt.rstrip("%z"))
            dt = dt.replace(tzinfo=UTC)
            return _format_relative_search_date(dt)
        except ValueError:
            continue
    return raw
