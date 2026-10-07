"""Give ``TripCalendarLink.event_fingerprint`` a database default, so the release before 0067 can keep inserting links.

A deploy migrates while the previous release still runs, and its inserts name only the columns it knows; 0067 added
this NOT NULL column with only a Python default.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0068_rate_limit_rows_from_any_release_take_0_9_0_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tripcalendarlink",
            name="event_fingerprint",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
    ]
