"""Add pin_type and parent_pin fields; replace unique_together with conditional UniqueConstraint."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0022_uuid_fields"),
    ]

    operations = [
        # Drop the old unconditional unique_together first.
        migrations.AlterUniqueTogether(
            name="pin",
            unique_together=set(),
        ),
        # Add pin_type.
        migrations.AddField(
            model_name="pin",
            name="pin_type",
            field=models.CharField(
                choices=[
                    ("location", "Location"),
                    ("building", "Building"),
                    ("entrance", "Entrance"),
                    ("poi", "Point of Interest"),
                    ("danger", "Danger"),
                    ("other", "Other"),
                ],
                default="location",
                max_length=30,
            ),
        ),
        # Add parent_pin self-FK.
        migrations.AddField(
            model_name="pin",
            name="parent_pin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="detail_pins",
                to="dashboard.pin",
            ),
        ),
        # Index on parent_pin for fast child lookups.
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["parent_pin"], name="dashboard_pin_parent_idx"),
        ),
        # Conditional UniqueConstraint replaces unique_together.
        migrations.AddConstraint(
            model_name="pin",
            constraint=models.UniqueConstraint(
                condition=models.Q(parent_pin__isnull=True),
                fields=["latitude", "longitude", "profile"],
                name="dashboard_pin_unique_location_per_profile",
            ),
        ),
    ]
