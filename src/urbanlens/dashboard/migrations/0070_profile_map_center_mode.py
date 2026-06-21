from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0069_profile_map_center"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="map_center_mode",
            field=models.CharField(
                choices=[("auto", "Center on my pins"), ("gps", "Use my current location"), ("custom", "Custom location")],
                default="auto",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="map_custom_latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="map_custom_longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="map_default_zoom",
            field=models.IntegerField(default=13),
        ),
    ]
