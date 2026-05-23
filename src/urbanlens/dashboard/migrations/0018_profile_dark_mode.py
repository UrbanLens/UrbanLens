from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0017_alter_profile_avatar"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="dark_mode",
            field=models.BooleanField(default=False),
        ),
    ]
