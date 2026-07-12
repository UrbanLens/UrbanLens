"""Google Photos plugin: rate-limit defaults for the per-user picker integration.

The integration itself (OAuth connect/disconnect, pin-detail picker session)
lives in ``dashboard/services/apis/photos/``, ``dashboard/controllers/google_photos.py``,
and ``dashboard/tasks.py``; every call runs against the requesting user's own
Google Photos library using their stored OAuth grant (a separate grant from
Google Calendar - see ``GooglePhotosAccount``). This plugin registers the
service's rate-limit defaults so calls are throttled and logged like every
other external API.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class GooglePhotosPlugin(UrbanLensPlugin):
    """Google Photos integration: per-user Picker-API photo import."""

    name: ClassVar[str] = "google_photos"
    verbose_name: ClassVar[str] = "Google Photos"
    description: ClassVar[str] = "Lets a user pick photos from their Google Photos library (via Google's own Picker UI) to import onto a pin."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 42

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google Photos Picker API.

        Returns:
            Defaults for the ``google_photos`` service key.
        """
        return {
            "google_photos": ServiceDefaults(
                display_name="Google Photos Picker API",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Free API; Google quota is per-user. Session polling is the bulk of the call volume.",
            ),
        }
