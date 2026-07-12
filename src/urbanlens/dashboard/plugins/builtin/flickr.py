"""Flickr plugin: rate-limit defaults for the per-user photo import integration.

The integration itself (OAuth1 connect/disconnect, pin-detail search and
import) lives in ``dashboard/services/apis/flickr/``,
``dashboard/controllers/flickr.py``, and ``dashboard/tasks.py``; every call
runs against the requesting user's own Flickr account using their stored
OAuth1 token pair. This plugin registers the service's rate-limit defaults so
calls are throttled and logged like every other external API.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class FlickrPlugin(UrbanLensPlugin):
    """Flickr integration: per-user photo search and import."""

    name: ClassVar[str] = "flickr"
    verbose_name: ClassVar[str] = "Flickr"
    description: ClassVar[str] = "Searches a user's own Flickr photos near a pin (server-side geo radius) and imports selected ones."
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
