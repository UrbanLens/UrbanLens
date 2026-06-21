from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0071_profile_use_pin_cache"),
    ]

    operations = [
        migrations.AlterField(
            model_name="profile",
            name="map_center_mode",
            field=models.CharField(
                choices=[("auto", "Auto"), ("gps", "Gps"), ("custom", "Custom")],
                default="gps",
                max_length=10,
            ),
        ),
    ]
