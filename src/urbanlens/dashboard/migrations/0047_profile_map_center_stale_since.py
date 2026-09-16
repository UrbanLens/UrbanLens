from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0046_pin_profile_created_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="map_center_stale_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
