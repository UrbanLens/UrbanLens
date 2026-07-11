"""Google Calendar plugin: rate-limit defaults for the per-user calendar sync.

The integration itself (OAuth connect, trip import/export) lives in
``dashboard/services/apis/calendar/google.py`` and
``dashboard/services/calendar_sync.py``; every call runs against the
requesting user's own calendar using their stored OAuth grant. This plugin
registers the service's rate-limit defaults so calls are throttled and
logged like every other external API.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class GoogleCalendarPlugin(UrbanLensPlugin):
    """Google Calendar integration: per-user trip import/export."""

    name: ClassVar[str] = "google_calendar"
    verbose_name: ClassVar[str] = "Google Calendar"
    description: ClassVar[str] = "Imports a user's calendar events as trips and exports trips to their Google Calendar."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 30

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Google Calendar API.

        Returns:
            Defaults for the ``google_calendar`` service key.
        """
        return {
            "google_calendar": ServiceDefaults(
                display_name="Google Calendar API",
                calls_per_minute=30,
                calls_per_day=2000,
                notes="Free API; Google quota is per-user (default 600 queries/min/user across the project).",
            ),
        }
