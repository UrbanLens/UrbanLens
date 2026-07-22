"""Flickr plugin: rate-limit defaults for all three Flickr integrations.

Three integrations share this one ``flickr`` service key and rate-limit
budget: one user's own OAuth1-connected library (search/import on a pin's
Media tab), unauthenticated import of any public Flickr album/photoset by URL
(pin or wiki Media), and an unauthenticated, required-operator public search
that contributes a Media gallery tab like Wikimedia/Smithsonian/LOC. The first
two live under ``dashboard/controllers/flickr.py`` and ``dashboard/tasks.py``;
the third is ``services/apis/flickr/search.py``. This plugin registers the
shared rate-limit defaults so calls are throttled and logged like every other
external API, and contributes the search provider's panel source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.services.external_data import PanelSource


class FlickrPlugin(UrbanLensPlugin):
    """Flickr integration: per-user photo search/import, public album import, and public search."""

    name: ClassVar[str] = "flickr"
    verbose_name: ClassVar[str] = "Flickr"
    description: ClassVar[str] = (
        "Searches a user's own Flickr photos near a pin (server-side geo radius) and imports selected ones; "
        "imports any public Flickr album/photoset by URL onto a pin or wiki, no account connection required; "
        "and adds a required-operator public Flickr search to the pin detail page's Media gallery."
    )
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 41

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Flickr API.

        Returns:
            Defaults for the ``flickr`` service key.
        """
        return {
            "flickr": ServiceDefaults(
                display_name="Flickr API",
                calls_per_minute=30,
                calls_per_day=3000,
                notes="Free API; Flickr's own per-key quota is generous, this mainly guards against runaway loops.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the public Flickr search Media gallery provider.

        Uses the full-text ``flickr.photos.search`` API when a Flickr API key
        is configured, or falls back to Flickr's keyless public tags feed
        otherwise (weaker recall - see ``services.apis.flickr.search``'s
        module docstring). Re-checked on every fetch, so the provider
        upgrades to the API-backed one transparently the moment a key is
        added, with no restart or code change needed.
        """
        from urbanlens.dashboard.services.apis.flickr.oauth import is_configured
        from urbanlens.dashboard.services.apis.flickr.search import FlickrFeedSearchGateway, FlickrMediaPanelSource, FlickrSearchGateway

        def _gateway_factory() -> FlickrSearchGateway | FlickrFeedSearchGateway:
            return FlickrSearchGateway() if is_configured() else FlickrFeedSearchGateway()

        return [FlickrMediaPanelSource("flickr", FlickrSearchGateway.service_key, _gateway_factory)]
