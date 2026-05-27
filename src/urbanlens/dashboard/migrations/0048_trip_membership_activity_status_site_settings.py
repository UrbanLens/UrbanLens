"""Add TripMembership through-model, TripActivity.status, and SiteSettings."""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def copy_memberships(apps, schema_editor):
    """Copy existing implicit M2M rows into the new TripMembership table."""
    db = schema_editor.connection.alias
    TripMembership = apps.get_model("dashboard", "TripMembership")
    Trip = apps.get_model("dashboard", "Trip")

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT trip_id, profile_id FROM dashboard_trips_profiles")
        rows = cursor.fetchall()

    existing = set(
        TripMembership.objects.using(db).values_list("trip_id", "profile_id")
    )
    to_create = []
    for trip_id, profile_id in rows:
        if (trip_id, profile_id) not in existing:
            to_create.append(TripMembership(trip_id=trip_id, profile_id=profile_id))

    if to_create:
        TripMembership.objects.using(db).bulk_create(to_create)

    # Set creator RSVP to "going"
    for trip in Trip.objects.using(db).select_related("creator").filter(creator__isnull=False):
        TripMembership.objects.using(db).filter(
            trip=trip, profile=trip.creator
        ).update(rsvp="yes")


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0047_remove_badgecustomization_unique_tag_customization_per_profile_and_more"),
    ]

    operations = [
        # ── TripActivity.status ───────────────────────────────────────────────
        migrations.AddField(
            model_name="tripactivity",
            name="status",
            field=models.CharField(
                choices=[("proposed", "Proposed"), ("confirmed", "Confirmed")],
                default="proposed",
                max_length=20,
            ),
        ),
        # ── TripMembership through-model ──────────────────────────────────────
        migrations.CreateModel(
            name="TripMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "trip",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="dashboard.trip",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="trip_memberships",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "rsvp",
                    models.CharField(
                        blank=True,
                        choices=[("yes", "Yes"), ("no", "No"), ("maybe", "Maybe")],
                        max_length=20,
                        null=True,
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_trip_memberships",
                "unique_together": {("trip", "profile")},
            },
        ),
        migrations.AddIndex(
            model_name="tripmembership",
            index=models.Index(fields=["trip"], name="dashboard_tm_trip_idx"),
        ),
        # ── Migrate data + swap profiles M2M to use through model ─────────────
        migrations.RunPython(copy_memberships, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="DROP TABLE IF EXISTS dashboard_trips_profiles",
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AlterField(
                    model_name="trip",
                    name="profiles",
                    field=models.ManyToManyField(
                        blank=True,
                        related_name="trips",
                        through="dashboard.TripMembership",
                        to="dashboard.profile",
                    ),
                ),
            ],
        ),
        # ── SiteSettings singleton ────────────────────────────────────────────
        migrations.CreateModel(
            name="SiteSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "max_trip_members",
                    models.IntegerField(
                        default=10,
                        help_text="Maximum number of members allowed per trip.",
                    ),
                ),
            ],
            options={
                "verbose_name": "Site Settings",
                "verbose_name_plural": "Site Settings",
                "db_table": "dashboard_site_settings",
            },
        ),
    ]
