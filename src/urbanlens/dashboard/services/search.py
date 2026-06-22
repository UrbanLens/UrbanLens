"""Search provider factory — returns the configured gateway for web search."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


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
    from urbanlens.dashboard.models.site_settings import (
        SEARCH_PROVIDER_BRAVE,
        SEARCH_PROVIDER_GOOGLE,
        SiteSettings,
    )

    try:
        provider = SiteSettings.get_current().search_provider
    except Exception:
        provider = SEARCH_PROVIDER_BRAVE

    if provider == SEARCH_PROVIDER_GOOGLE:
        from urbanlens.dashboard.services.google.search import GoogleCustomSearchGateway
        return GoogleCustomSearchGateway()

    from urbanlens.dashboard.services.brave.search import BraveSearchGateway
    return BraveSearchGateway()
