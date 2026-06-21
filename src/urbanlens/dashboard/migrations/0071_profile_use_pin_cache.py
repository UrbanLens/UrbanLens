from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0070_profile_map_center_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="use_pin_cache",
            field=models.BooleanField(default=True),
        ),
    ]
