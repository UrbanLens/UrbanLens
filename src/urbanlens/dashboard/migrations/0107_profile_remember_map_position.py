"""Add MapCenterMode.REMEMBER support to Profile.

Stores the last map position (lat/lng/zoom) the user left so it can be
restored on the next page load when map_center_mode is 'remember'.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0106_pinmarkup_security_indicator"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="remembered_map_lat",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="remembered_map_lng",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="remembered_map_zoom",
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="profile",
            name="map_center_mode",
            field=models.CharField(
                choices=[
                    ("auto", "Center on my pins"),
                    ("gps", "Use my current location"),
                    ("custom", "Custom location"),
                    ("remember", "Remember last position"),
                ],
                default="gps",
                max_length=10,
            ),
        ),
    ]
