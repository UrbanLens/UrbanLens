"""Google Calendar plugin: rate-limit defaults for the per-user calendar sync.
This plugin registers the service's rate-limit defaults so calls are throttled and logged like every other external API."""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

#: Site-wide calls a minute. At least ``max_trip_activities + 1`` at default settings, so one export of a full trip
#: finishes in one attempt; an export that does run out is finished by a push (``services.trips.calendar_sync``).
GOOGLE_CALENDAR_CALLS_PER_MINUTE = 120


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
                category=EgressCategory.PUBLIC_WRITE,
                calls_per_minute=GOOGLE_CALENDAR_CALLS_PER_MINUTE,
                calls_per_day=2000,
                notes=("Free API; Google's quota is per user (600 a minute by default), so this is our own politeness limit. A minute holds one whole trip export: an event per activity (max_trip_activities, 100 by default) plus the trip's own."),
            ),
        }
