"""Let a trip's calendar export resume where it stopped, and give the calendar budget room for one whole trip (P334).

``TripCalendarLink.event_fingerprint`` records what was last written to each event, so an export skips the events
that already match and an attempt the budget cut short is resumed rather than restarted. Existing links start blank,
so each trip's next export rewrites its events once.

``google_calendar``'s default went from 30 calls a minute to 120, so one export of a trip at ``max_trip_activities``
(100 by default, plus the trip's own event) fits in a minute. ``rate_limiter.get_limit_config`` never rewrites a row
holding a service's own defaults, since an admin may have chosen them, so as in 0064 the new values are written only
into a row that still holds exactly what every release since 0.4.0 wrote. The daily limit stays 2,000. An edited row
is left alone and logged.

Reverse drops the field and leaves the row: a row at 120 cannot be told from one an admin set to 120.
"""

import logging

from django.db import migrations, models
from django.utils import timezone

logger = logging.getLogger(__name__)

#: The fields ``get_limit_config`` writes from a service's defaults; ``enabled`` is not one of them.
_FIELDS = ("display_name", "calls_per_minute", "calls_per_day", "calls_per_30_days", "min_interval_seconds", "usa_only", "notes")

#: What every release up to 0.9.0 wrote for ``google_calendar``, frozen as it was.
_OLD = {
    "display_name": "Google Calendar API",
    "calls_per_minute": 30,
    "calls_per_day": 2000,
    "calls_per_30_days": None,
    "min_interval_seconds": None,
    "usa_only": False,
    "notes": "Free API; Google quota is per-user (default 600 queries/min/user across the project).",
}

#: What 0.9.0 writes, frozen as it is.
_NEW = {
    "calls_per_minute": 120,
    "notes": (
        "Free API; Google's quota is per user (600 a minute by default), so this is our own politeness limit. "
        "A minute holds one whole trip export: an event per activity (max_trip_activities, 100 by default) plus the trip's own."
    ),
}


def raise_calendar_minute_limit(apps, schema_editor):
    """Write the new per-minute limit into a ``google_calendar`` row that still holds exactly the old defaults."""
    ApiRateLimit = apps.get_model("dashboard", "ApiRateLimit")
    if ApiRateLimit.objects.filter(service="google_calendar", **{field: _OLD[field] for field in _FIELDS}).update(**_NEW, updated=timezone.now()):
        return
    kept = ApiRateLimit.objects.filter(service="google_calendar").values_list("calls_per_minute", flat=True).first()
    if kept is not None and kept != _NEW["calls_per_minute"]:
        logger.warning("google_calendar keeps %s calls a minute, not 0.9.0's %s: its row was edited, so it stays as its admin left it", kept, _NEW["calls_per_minute"])


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0066_smart_list_sync_requests"),
    ]

    operations = [
        migrations.AddField(
            model_name="tripcalendarlink",
            name="event_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(code=raise_calendar_minute_limit, reverse_code=migrations.RunPython.noop),
    ]
