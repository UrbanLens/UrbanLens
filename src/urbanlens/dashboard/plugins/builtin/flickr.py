"""
Flickr plugin: rate-limit defaults for all three Flickr integrations.
This plugin registers the shared rate-limit defaults so calls are throttled and logged like every other external API, and contributes the search provider's panel source."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class FlickrPlugin(UrbanLensPlugin):
    """Flickr integration: per-user photo search/import, public album import, and public search."""

    name: ClassVar[str] = "flickr"
    verbose_name: ClassVar[str] = "Flickr"
    description: ClassVar[str] = (
        "Searches a user's own Flickr photos near a pin (server-side geo radius) and imports selected ones; "
        "imports any public Flickr album/photoset by URL onto a pin or wiki, no account connection required; "
        "and adds a required-operator public Flickr search to the Private Pin page's Media gallery."
    )
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 41

    def setup(self, plugin: PluginContext) -> None:
        """Rate-limit defaults for the Flickr API, and the public Flickr search Media gallery provider."""
        from urbanlens.dashboard.plugins.points import PANEL_SOURCES, SERVICE_DEFAULTS
        from urbanlens.dashboard.services.apis.flickr import oauth
        from urbanlens.dashboard.services.apis.flickr.search import FlickrFeedSearchGateway, FlickrMediaPanelSource, FlickrSearchGateway

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "flickr": ServiceDefaults(
                    display_name="Flickr API",
                    category=EgressCategory.QUOTA,
                    calls_per_minute=30,
                    calls_per_day=3000,
                    notes="Free API; Flickr's own per-key quota is generous, this mainly guards against runaway loops.",
                ),
            },
        )

        # Full-text search with an API key; otherwise Flickr's keyless public tags feed, which has weaker recall.
        def _gateway_factory() -> FlickrSearchGateway | FlickrFeedSearchGateway:
            return FlickrSearchGateway() if oauth.is_configured() else FlickrFeedSearchGateway()

        plugin.extend(PANEL_SOURCES, [FlickrMediaPanelSource("flickr", FlickrSearchGateway.service_key, _gateway_factory)])
