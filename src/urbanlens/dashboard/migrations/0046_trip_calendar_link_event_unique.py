"""One Google Calendar event maps to at most one link per profile.

Without this, `import_events_to_trips`' check-then-create could run twice for
the same event - a double-submit, or two workers - and build a second Trip for
an event the profile had already imported.

The data step drops only the *duplicate link rows*, keeping the oldest one per
(profile, event). It deliberately does not delete the extra Trips those links
pointed at: a trip may already carry the user's own activities, members or
comments, so it is unlinked rather than destroyed and remains editable as an
ordinary trip. Keeping the oldest favours the trip the user has had longest.
"""

from django.db import migrations, models
from django.db.models import Q


def drop_duplicate_event_links(apps, schema_editor):
    """Unlink all but the oldest link per (profile, non-empty google_event_id).

    Args:
        apps: Historical app registry.
        schema_editor: Unused; required by the RunPython signature.
    """
    TripCalendarLink = apps.get_model("dashboard", "TripCalendarLink")

    seen: set[tuple[int, str]] = set()
    doomed: list[int] = []
    rows = TripCalendarLink.objects.exclude(google_event_id="").order_by("created", "pk").values_list("pk", "profile_id", "google_event_id")
    for pk, profile_id, event_id in rows.iterator():
        key = (profile_id, event_id)
        if key in seen:
            doomed.append(pk)
        else:
            seen.add(key)

    if doomed:
        TripCalendarLink.objects.filter(pk__in=doomed).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0045_site_settings_login_ip_max_attempts"),
    ]

    operations = [
        migrations.RunPython(drop_duplicate_event_links, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="tripcalendarlink",
            constraint=models.UniqueConstraint(
                fields=("profile", "google_event_id"),
                condition=~Q(google_event_id=""),
                name="db_tcl_profile_event_unique",
            ),
        ),
    ]
