"""Clear activity times outside the span 0093 constrains them to, so the constraint can be added."""

import datetime

from django.db import migrations
from django.db.models import Q

_EARLIEST = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)
_LATEST = datetime.datetime(2200, 1, 1, tzinfo=datetime.UTC)


def _clear_out_of_span(apps, schema_editor) -> None:
    TripActivity = apps.get_model("dashboard", "TripActivity")
    for field in ("scheduled_at", "scheduled_end"):
        TripActivity.objects.filter(Q(**{f"{field}__lt": _EARLIEST}) | Q(**{f"{field}__gte": _LATEST})).update(**{field: None})


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0091_move_recorded_weather_to_day_rows"),
    ]

    operations = [
        migrations.RunPython(_clear_out_of_span, migrations.RunPython.noop),
    ]
