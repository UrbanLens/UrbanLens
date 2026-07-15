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
        """Rate-limit defaults for a self-hosted/trusted SearXNG instance."""
        return {
            "searxng": ServiceDefaults(
                display_name="SearXNG",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless, self-hosted metasearch. Limits depend entirely on the configured instance's own capacity.",
            ),
        }
