"""Move recorded weather out of one JSON document per Location into one row per cell and day."""

from datetime import date

from django.db import migrations

_SOURCE = "redata_weather_history"


def _move_to_day_rows(apps, schema_editor) -> None:
    LocationCache = apps.get_model("dashboard", "LocationCache")
    RecordedWeatherDay = apps.get_model("dashboard", "RecordedWeatherDay")

    rows: dict[tuple[int, int, date], dict] = {}
    entries = LocationCache.objects.filter(source=_SOURCE).select_related("location")
    for entry in entries.iterator(chunk_size=200):
        location = entry.location
        if location.latitude is None or location.longitude is None or not isinstance(entry.data, dict):
            continue
        cell = (round(float(location.latitude) * 100), round(float(location.longitude) * 100))
        for iso, record in entry.data.items():
            try:
                day = date.fromisoformat(iso)
            except (TypeError, ValueError):
                continue
            if isinstance(record, dict):
                rows.setdefault((*cell, day), record)
    RecordedWeatherDay.objects.bulk_create(
        [RecordedWeatherDay(cell_lat=lat, cell_lng=lng, day=day, data=data) for (lat, lng, day), data in rows.items()],
        batch_size=1000,
    )
    LocationCache.objects.filter(source=_SOURCE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0090_recordedweatherday"),
    ]

    # The old documents are a cache REData can refill, so the reverse leaves them gone.
    operations = [
        migrations.RunPython(_move_to_day_rows, migrations.RunPython.noop),
    ]
