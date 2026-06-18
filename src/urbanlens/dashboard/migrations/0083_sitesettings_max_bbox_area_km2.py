from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0082_pin_remove_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="max_bbox_area_km2",
            field=models.FloatField(
                default=2600.0,
                help_text="Maximum allowed area (km²) for a location bounding box. Default ≈ Chernobyl Exclusion Zone.",
            ),
        ),
    ]
