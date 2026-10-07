"""Record which trip stop titles name their place, and queue the deletes of calendar events whose stop is gone.

``TripActivity.title_from_place`` marks a title taken from the place: a place search's name, stored as the title when
none was typed (P186), or an imported calendar event's location. Such a title is withheld wherever the stop's
location is; see UrbanLens#303 ("a hidden stop shows its place's name"). Nothing recorded where an existing title came from, so every title that may be a place's is
marked: every located stop's, since a place search always makes a Location, and an unlocated stop's only when a
calendar import made it (its note, or the import's link to it). A hidden stop's typed title then reads "Secret
Location" to members who may not see the stop, until its author types a title again; a visible stop shows its title
as before, and a stop with no place keeps showing its typed title to everyone.

``CalendarEventDeletion`` holds the delete owed for an event UrbanLens made whose trip or activity was deleted, since
the link that named it goes with them; see UrbanLens#301 ("hidden location stays on an unreached calendar").

Reverse drops both; the marks cannot be told from typed titles afterwards.
"""

from django.db import migrations, models
import django.db.models.deletion

#: The note ``calendar_sync._create_activity_from_event`` has given every activity it made, since 0.4.0.
_IMPORT_NOTE = "Location from the imported Google Calendar event."


def mark_stored_titles_as_possibly_the_places(apps, schema_editor):
    """Mark every title that may be a place's: a located stop's, and an imported event's location."""
    TripActivity = apps.get_model("dashboard", "TripActivity")
    TripCalendarLink = apps.get_model("dashboard", "TripCalendarLink")
    imported = TripCalendarLink.objects.filter(direction="imported", activity__isnull=False).values("activity_id")
    may_name_a_place = models.Q(location__isnull=False) | models.Q(notes=_IMPORT_NOTE) | models.Q(pk__in=imported)
    TripActivity.objects.filter(may_name_a_place, title__regex=r"\S").update(title_from_place=True)


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
