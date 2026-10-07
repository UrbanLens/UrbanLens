"""Record which trip stop titles name their place, and queue the deletes of calendar events whose stop is gone.

``TripActivity.title_from_place`` marks a title taken from the place: a place search's name, stored as the title when
none was typed (P186), or an imported calendar event's location. Such a title is withheld wherever the stop's
location is (P338). Nothing recorded where an existing title came from, so every stored title is marked: a hidden
stop's typed title then reads "Secret Location" to members who may not see the stop, until its author types a title
again. A visible stop shows its title as before.

``CalendarEventDeletion`` holds the delete owed for an event UrbanLens made whose trip or activity was deleted, since
the link that named it goes with them (P336).

Reverse drops both; the marks cannot be told from typed titles afterwards.
"""

from django.db import migrations, models
import django.db.models.deletion


def mark_stored_titles_as_possibly_the_places(apps, schema_editor):
    """Mark every stop with a title: none can be shown to have been typed."""
    TripActivity = apps.get_model("dashboard", "TripActivity")
    TripActivity.objects.filter(title__regex=r"\S").update(title_from_place=True)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0068_rate_limit_rows_from_any_release_take_0_9_0_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="tripactivity",
            name="title_from_place",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(code=mark_stored_titles_as_possibly_the_places, reverse_code=migrations.RunPython.noop),
        migrations.CreateModel(
            name="CalendarEventDeletion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("google_calendar_id", models.CharField(default="primary", max_length=255)),
                ("google_event_id", models.CharField(max_length=1024)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="calendar_event_deletions", to="dashboard.profile")),
            ],
            options={
                "db_table": "dashboard_calendar_event_deletions",
                "abstract": False,
                "constraints": [models.UniqueConstraint(fields=("profile", "google_calendar_id", "google_event_id"), name="db_ced_profile_event_unique")],
            },
        ),
    ]
