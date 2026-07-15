"""SearXNG plugin: open-source, self-hostable metasearch web search provider."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class SearxngPlugin(UrbanLensPlugin):
    """Adds SearXNG as a selectable web-search provider (Settings > Search)."""

    name: ClassVar[str] = "searxng"
    verbose_name: ClassVar[str] = "SearXNG"
    description: ClassVar[str] = (
        "Free, open-source metasearch engine you self-host or point at a trusted instance - "
        "no API key, aggregates results from many upstream engines. Configure UL_SEARXNG_BASE_URL."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for a self-hosted/trusted SearXNG instance.

        No daily cap: it's our own infrastructure, not a metered third-party
        quota. The per-minute limit stays conservative instead, since a
        single SearXNG query fans out to several upstream engines (Google,
        Bing, Brave, DuckDuckGo, etc.) that could rate-limit or block our
        instance's IP if hit too fast - that's the only real constraint here.
        """
        return {
            "searxng": ServiceDefaults(
                display_name="SearXNG",
                calls_per_minute=20,
                calls_per_day=None,
                notes="Free, keyless, self-hosted metasearch - no daily cap since it's our own infrastructure. The per-minute limit protects the upstream engines SearXNG scrapes on our behalf, not our own capacity.",
            ),
        }
