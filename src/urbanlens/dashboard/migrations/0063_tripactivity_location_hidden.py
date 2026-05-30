from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0062_alter_profile_friend_request_visibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="tripactivity",
            name="location_hidden",
            field=models.BooleanField(
                default=False,
                help_text="Hide location from the map. The activity still appears in the list as 'Secret Location'.",
            ),
        ),
    ]
