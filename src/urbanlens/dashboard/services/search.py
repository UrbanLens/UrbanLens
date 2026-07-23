"""Search provider factory and pin search query helpers."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from django.db import DatabaseError

from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


@runtime_checkable
class SearchGateway(Protocol):
    """Minimal interface shared by all search gateway implementations."""

    def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]: ...


# Default fallback order when no provider is explicitly preferred: SearXNG
# first (self-hosted, no external quota to protect), then the two paid/quota-
# limited general engines, then the remaining free/keyless-or-low-volume
# options as a last resort.
def _default_provider_order() -> list[str]:
    from urbanlens.dashboard.models.site_settings import SearchProviderChoice

    return [
        SearchProviderChoice.SEARXNG,
        SearchProviderChoice.GOOGLE,
        SearchProviderChoice.BRAVE,
        SearchProviderChoice.DUCKDUCKGO,
        SearchProviderChoice.MOJEEK,
        SearchProviderChoice.MARGINALIA,
    ]


def _build_gateway(provider: str) -> SearchGateway:
    """Instantiate the gateway for one provider slug.

    Args:
        provider: A ``SearchProviderChoice`` value.

    Returns:
        The matching gateway instance. Unknown values fall back to Brave.
    """
    from urbanlens.dashboard.models.site_settings import SearchProviderChoice

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


def get_search_gateway() -> SearchGateway:
    """Return the search gateway selected in SiteSettings.

    Defaults to Brave if the database row does not exist yet. Prefer
    ``search_web()`` for actually performing a search - it automatically
    falls back through every configured provider instead of failing outright
    when this one selected gateway is unconfigured or rate-limited.

    Returns:
        A gateway instance whose ``search(query)`` method returns a list of
        ``{"title": ..., "link": ..., "snippet": ...}`` dicts.
    """
    from urbanlens.dashboard.models.site_settings import SearchProviderChoice, SiteSettings

    try:
        provider = SiteSettings.get_current().search_provider
    except (ImportError, DatabaseError, Exception):
        provider = SearchProviderChoice.BRAVE

    return _build_gateway(provider)


def get_search_gateways() -> list[SearchGateway]:
    """Return every search gateway, ordered for automatic fallback.

    Default order is SearXNG, Google, Brave, then DuckDuckGo/Mojeek/
    Marginalia. The admin's manually selected ``search_provider`` (Settings >
    Web Search) is promoted to the front of that order when set, since an
    explicit choice should always be tried first - the rest of the chain
    still runs if it fails.

    Returns:
        Gateway instances in the order ``search_web()`` should try them.
    """
    from urbanlens.dashboard.models.site_settings import SiteSettings

    try:
        preferred = SiteSettings.get_current().search_provider
    except (ImportError, DatabaseError, Exception):
        preferred = None

    order = _default_provider_order()
    if preferred:
        if preferred in order:
            order.remove(preferred)
        order.insert(0, preferred)

    return [_build_gateway(provider) for provider in order]


def search_web(query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    """Search using the first available provider, falling back automatically.

    Tries each provider from ``get_search_gateways()`` in order, moving to
    the next one whenever a provider is unconfigured, rate-limited, disabled,
    or its request otherwise fails - so a single provider outage never blocks
    the "Web Search" pin panel outright.

    Args:
        query: The search string.
        max_results: Maximum number of results to request.

    Returns:
        Result dicts from the first provider that returns successfully.

    Raises:
        Exception: Re-raises the last provider's error if every provider failed.
    """
    last_error: Exception | None = None
    for gateway in get_search_gateways():
        try:
            return gateway.search(query, max_results=max_results)
        except Exception as exc:
            logger.warning("Search provider %s unavailable, trying next: %s", type(gateway).__name__, exc)
            last_error = exc
    if last_error is not None:
        raise last_error
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
            dt = datetime.strptime(raw[:19].rstrip("Z"), fmt.rstrip("%z"))
            dt = dt.replace(tzinfo=UTC)
            return _format_relative_search_date(dt)
        except ValueError:
            continue
    return raw
