from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0083_sitesettings_max_bbox_area_km2"),
    ]

    operations = [
        migrations.AddField(
            model_name="comment",
            name="map_data",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
