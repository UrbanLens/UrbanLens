"""RecordedWeatherDay - one day of recorded (reanalysis) weather at one grid cell."""

from __future__ import annotations

from django.db import models

from urbanlens.dashboard.models import abstract


class RecordedWeatherDay(abstract.DashboardModel):
    """What the weather was on one day in one 0.01° cell, as REData's ``/weather/history/`` reported it.

    Keyed by cell rather than by Location so a trip activity with only a coordinate shares rows with
    the Locations around it; ERA5's own grid is 0.25°, so nothing finer is lost. A past day never
    changes, so a row is written once and never refreshed.

    Attributes:
        cell_lat: Latitude in hundredths of a degree.
        cell_lng: Longitude in hundredths of a degree.
        day: The day the readings describe.
        data: REData's row for that day, in its fixed units.
    """

    cell_lat = models.IntegerField()
    cell_lng = models.IntegerField()
    day = models.DateField()
    data = models.JSONField(default=dict)

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_recorded_weather_day"
        constraints = [
            models.UniqueConstraint(fields=["cell_lat", "cell_lng", "day"], name="db_weatherday_cell_day_uniq"),
        ]

    def __str__(self) -> str:
        return f"{self.cell_lat / 100:.2f},{self.cell_lng / 100:.2f} {self.day.isoformat()}"
