"""Add ProfileTrustSignals, the precomputed real-user and independence inputs (ADR35 section 5)."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_describe_seeded_labels"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProfileTrustSignals",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("computed_at", models.DateTimeField()),
                ("account_age_days", models.PositiveIntegerField(default=0)),
                ("active_days_90", models.PositiveIntegerField(default=0)),
                ("pin_count", models.PositiveIntegerField(default=0)),
                ("pin_cells", models.JSONField(default=list)),
                ("region_count", models.PositiveIntegerField(default=0)),
                ("vouch_count", models.PositiveIntegerField(default=0)),
                ("is_real_user", models.BooleanField(default=False)),
                ("device_track_visits", models.PositiveIntegerField(default=0)),
                ("photo_visits", models.PositiveIntegerField(default=0)),
                ("recorded_visits", models.PositiveIntegerField(default=0)),
                ("profile", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="trust_signals", to="dashboard.profile")),
            ],
            options={
                "db_table": "dashboard_profile_trust_signals",
                "abstract": False,
            },
        ),
    ]
