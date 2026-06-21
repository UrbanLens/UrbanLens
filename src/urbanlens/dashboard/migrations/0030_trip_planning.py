"""Add uuid + creator to Trip; add TripActivity and TripComment models."""

import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0029_merge_heads"),
    ]

    operations = [
        # ── Expand Trip ──────────────────────────────────────────────────────
        migrations.AddField(
            model_name="trip",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True),
        ),
        migrations.RunSQL(
            sql='UPDATE "dashboard_trips" SET "uuid" = gen_random_uuid() WHERE "uuid" IS NULL',
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AlterField(
            model_name="trip",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="trip",
            name="name",
            field=models.CharField(max_length=255),
        ),
        migrations.AlterField(
            model_name="trip",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="trip",
            name="start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="trip",
            name="end_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="trip",
            name="creator",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_trips",
                to="dashboard.profile",
            ),
        ),
        migrations.AddIndex(
            model_name="trip",
            index=models.Index(fields=["uuid"], name="dashboard_trip_uuid_idx"),
        ),
        # ── TripActivity ─────────────────────────────────────────────────────
        migrations.CreateModel(
            name="TripActivity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("title", models.CharField(blank=True, max_length=255, null=True)),
                ("notes", models.TextField(blank=True, null=True)),
                ("scheduled_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.IntegerField(default=0)),
                (
                    "trip",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="activities",
                        to="dashboard.trip",
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="trip_activities",
                        to="dashboard.location",
                    ),
                ),
                (
                    "pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="trip_activities",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "added_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="trip_activities_added",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_trip_activities",
                "ordering": ["scheduled_at", "order", "created"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="tripactivity",
            index=models.Index(fields=["trip"], name="dashboard_ta_trip_idx"),
        ),
        migrations.AddIndex(
            model_name="tripactivity",
            index=models.Index(fields=["trip", "scheduled_at"], name="dashboard_ta_trip_dt_idx"),
        ),
        # ── TripComment ──────────────────────────────────────────────────────
        migrations.CreateModel(
            name="TripComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("text", models.TextField()),
                (
                    "trip",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="comments",
                        to="dashboard.trip",
                    ),
                ),
                (
                    "author",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="trip_comments",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_trip_comments",
                "ordering": ["created"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="tripcomment",
            index=models.Index(fields=["trip"], name="dashboard_tc_trip_idx"),
        ),
        # ── Remove legacy pins M2M from Trip ─────────────────────────────────
        migrations.RemoveField(
            model_name="trip",
            name="pins",
        ),
    ]
