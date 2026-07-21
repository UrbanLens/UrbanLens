"""Flickr plugin: rate-limit defaults for both Flickr photo-import integrations.

Two integrations share this one ``flickr`` service key and rate-limit budget:
one user's own OAuth1-connected library (search/import on a pin's Media tab),
and unauthenticated import of any public Flickr album/photoset by URL (pin or
wiki Media). Both live under ``dashboard/services/apis/flickr/``,
``dashboard/controllers/flickr.py``, and ``dashboard/tasks.py``. This plugin
registers the shared rate-limit defaults so calls are throttled and logged
like every other external API.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class FlickrPlugin(UrbanLensPlugin):
    """Flickr integration: per-user photo search/import, plus public album import."""

    name: ClassVar[str] = "flickr"
    verbose_name: ClassVar[str] = "Flickr"
    description: ClassVar[str] = (
        "Searches a user's own Flickr photos near a pin (server-side geo radius) and imports selected ones; "
        "also imports any public Flickr album/photoset by URL onto a pin or wiki, no account connection required."
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
