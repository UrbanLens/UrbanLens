"""DuckDuckGo plugin: free, keyless Instant Answer web search provider."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class DuckDuckGoPlugin(UrbanLensPlugin):
    """Adds DuckDuckGo as a selectable web-search provider (Settings > Search)."""

    name: ClassVar[str] = "duckduckgo"
    verbose_name: ClassVar[str] = "DuckDuckGo"
    description: ClassVar[str] = "Free, keyless Instant Answer API - no signup required. Returns a topic abstract and related links rather than a full result set, so it works best as a lightweight supplementary source."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the DuckDuckGo Instant Answer API."""
        return {
            "duckduckgo": ServiceDefaults(
                display_name="DuckDuckGo Instant Answer API",
                calls_per_minute=20,
                calls_per_day=1000,
                notes="Free, keyless API. Be conservative - shared public infrastructure.",
            ),
        }
