"""Convert Trip permission booleans to 3-state CharFields, add is_organizer to TripMembership."""

from django.db import migrations, models

_PERMISSION_CHOICES = [
    ("none", "No one (creator only)"),
    ("organizers", "Organizers"),
    ("everyone", "Everyone"),
]


def migrate_permission_booleans(apps, schema_editor):
    """Copy old bool values to the new char fields: True → 'everyone', False → 'none'."""
    Trip = apps.get_model("dashboard", "Trip")
    for trip in Trip.objects.all():
        trip.allow_add_members_new = "everyone" if trip.allow_add_members_old else "none"
        trip.allow_add_activities_new = "everyone" if trip.allow_add_activities_old else "none"
        trip.allow_edit_activities_new = "everyone" if trip.allow_edit_activities_old else "none"
        trip.allow_comments_new = "everyone" if trip.allow_comments_old else "none"
        trip.save(update_fields=[
            "allow_add_members_new",
            "allow_add_activities_new",
            "allow_edit_activities_new",
            "allow_comments_new",
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0066_tripactivity_completed_status"),
    ]

    operations = [
        # ── Step 1: rename old boolean fields out of the way ─────────────────
        migrations.RenameField(model_name="trip", old_name="allow_add_members", new_name="allow_add_members_old"),
        migrations.RenameField(model_name="trip", old_name="allow_add_activities", new_name="allow_add_activities_old"),
        migrations.RenameField(model_name="trip", old_name="allow_edit_activities", new_name="allow_edit_activities_old"),
        migrations.RenameField(model_name="trip", old_name="allow_comments", new_name="allow_comments_old"),

        # ── Step 2: add new CharField fields ─────────────────────────────────
        migrations.AddField(
            model_name="trip",
            name="allow_add_members_new",
            field=models.CharField(choices=_PERMISSION_CHOICES, default="none", max_length=20),
        ),
        migrations.AddField(
            model_name="trip",
            name="allow_add_activities_new",
            field=models.CharField(choices=_PERMISSION_CHOICES, default="everyone", max_length=20),
        ),
        migrations.AddField(
            model_name="trip",
            name="allow_edit_activities_new",
            field=models.CharField(choices=_PERMISSION_CHOICES, default="everyone", max_length=20),
        ),
        migrations.AddField(
            model_name="trip",
            name="allow_comments_new",
            field=models.CharField(choices=_PERMISSION_CHOICES, default="everyone", max_length=20),
        ),

        # ── Step 3: data migration ────────────────────────────────────────────
        migrations.RunPython(migrate_permission_booleans, migrations.RunPython.noop),

        # ── Step 4: drop old boolean fields ──────────────────────────────────
        migrations.RemoveField(model_name="trip", name="allow_add_members_old"),
        migrations.RemoveField(model_name="trip", name="allow_add_activities_old"),
        migrations.RemoveField(model_name="trip", name="allow_edit_activities_old"),
        migrations.RemoveField(model_name="trip", name="allow_comments_old"),

        # ── Step 5: rename new fields to canonical names ──────────────────────
        migrations.RenameField(model_name="trip", old_name="allow_add_members_new", new_name="allow_add_members"),
        migrations.RenameField(model_name="trip", old_name="allow_add_activities_new", new_name="allow_add_activities"),
        migrations.RenameField(model_name="trip", old_name="allow_edit_activities_new", new_name="allow_edit_activities"),
        migrations.RenameField(model_name="trip", old_name="allow_comments_new", new_name="allow_comments"),

        # ── Step 6: add is_organizer to TripMembership ───────────────────────
        migrations.AddField(
            model_name="tripmembership",
            name="is_organizer",
            field=models.BooleanField(
                default=False,
                help_text="Organizers have the same trip-management rights as the creator.",
            ),
        ),
    ]
