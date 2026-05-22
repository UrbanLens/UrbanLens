"""Add Location FK to Pin; remove duplicate address fields from Pin; make name/coords nullable."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0003_location_cid"),
    ]

    operations = [
        # 1. Add the Location FK (nullable - existing pins have no location yet).
        migrations.AddField(
            model_name="pin",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pins",
                to="dashboard.location",
            ),
        ),

        # 2. Make name nullable (None = fall back to location.name).
        migrations.AlterField(
            model_name="pin",
            name="name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),

        # 3. Make coordinates nullable (None = fall back to location's coords).
        migrations.AlterField(
            model_name="pin",
            name="latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AlterField(
            model_name="pin",
            name="longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),

        # 4. Remove duplicate address fields - these now live on Location.
        migrations.RemoveField(model_name="pin", name="street_number"),
        migrations.RemoveField(model_name="pin", name="route"),
        migrations.RemoveField(model_name="pin", name="locality"),
        migrations.RemoveField(model_name="pin", name="administrative_area_level_1"),
        migrations.RemoveField(model_name="pin", name="administrative_area_level_2"),
        migrations.RemoveField(model_name="pin", name="administrative_area_level_3"),
        migrations.RemoveField(model_name="pin", name="country"),
        migrations.RemoveField(model_name="pin", name="zipcode"),
        migrations.RemoveField(model_name="pin", name="zipcode_suffix"),
        migrations.RemoveField(model_name="pin", name="cached_place_name"),
    ]
