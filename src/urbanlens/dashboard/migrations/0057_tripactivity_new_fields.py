"""Migration: add scheduled_end, child_trip, lat_override, lng_override to TripActivity."""

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """Add end datetime, child trip link, and map position override fields to TripActivity."""

    dependencies = [
        ("dashboard", "0056_profile_friend_request_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="tripactivity",
            name="scheduled_end",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tripactivity",
            name="child_trip",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="parent_activities",
                to="dashboard.trip",
            ),
        ),
        migrations.AddField(
            model_name="tripactivity",
            name="lat_override",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tripactivity",
            name="lng_override",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
