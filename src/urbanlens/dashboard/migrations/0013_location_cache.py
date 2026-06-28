from __future__ import annotations

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0012_pin_danger"),
    ]

    operations = [
        migrations.CreateModel(
            name="LocationCache",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="external_cache",
                        to="dashboard.location",
                    ),
                ),
                ("source", models.CharField(max_length=50)),
                ("data", models.JSONField(default=dict)),
                ("query_key", models.CharField(blank=True, max_length=255)),
            ],
            options={
                "db_table": "dashboard_location_cache",
                "unique_together": {("location", "source")},
            },
        ),
        migrations.AddIndex(
            model_name="locationcache",
            index=models.Index(fields=["location", "source"], name="dash_loccache_loc_src_idx"),
        ),
        migrations.RenameIndex(
            model_name="locationcache",
            new_name="dashboard_l_locatio_5f8691_idx",
            old_name="dash_loccache_loc_src_idx",
        ),
    ]
