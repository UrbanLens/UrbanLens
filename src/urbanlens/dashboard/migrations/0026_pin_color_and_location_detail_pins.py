"""Add color field to Pin and parent_location FK for community (wiki) detail pins."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0025_aliases"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="color",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="pin",
            name="parent_location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="location_detail_pins",
                to="dashboard.location",
            ),
        ),
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["parent_location"], name="dashboard_pin_parent_loc_idx"),
        ),
    ]
