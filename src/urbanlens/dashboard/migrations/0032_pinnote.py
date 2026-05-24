"""Add PinNote model — private per-pin notes for the pin owner."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0031_remove_pin_dashboard_pin_unique_location_per_profile_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("text", models.TextField()),
                (
                    "pin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="notes",
                        to="dashboard.pin",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_pin_notes",
                "ordering": ["-created"],
                "abstract": False,
            },
        ),
        migrations.AddIndex(
            model_name="pinnote",
            index=models.Index(fields=["pin"], name="dashboard_pn_pin_idx"),
        ),
    ]
