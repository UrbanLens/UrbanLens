from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0064_tripactivityvote"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="hide_pin_locations_in_trips",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When sharing one of your pins as a trip activity, hide the location "
                    "from members who don't already have that pin on their map."
                ),
            ),
        ),
    ]
