"""Give the columns 0067 and 0069 added database defaults, so the release before them can keep inserting rows.

A deploy migrates while the previous release still runs, and its inserts name only the columns it knows;
``TripCalendarLink.event_fingerprint`` (0067) and ``TripActivity.title_from_place`` (0069) were added NOT NULL with
only a Python default.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0070_withdraw_year_built_trivia"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tripcalendarlink",
            name="event_fingerprint",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="tripactivity",
            name="title_from_place",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
