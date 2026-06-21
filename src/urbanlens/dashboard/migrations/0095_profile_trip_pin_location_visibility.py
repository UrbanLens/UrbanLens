"""Replace the boolean hide_pin_locations_in_trips with a fine-grained
VisibilityChoice field called trip_pin_location_visibility.

Old False (show to everyone) → 'anyone'
Old True  (hide from non-pinners) → 'common_pin'
"""

from django.db import migrations, models


def _migrate_forward(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.filter(hide_pin_locations_in_trips=True).update(
        trip_pin_location_visibility="common_pin"
    )
    # False rows already have the default 'anyone' from AddField.


def _migrate_reverse(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    # Anything that isn't 'anyone' is treated as "hide" (True).
    Profile.objects.exclude(trip_pin_location_visibility="anyone").update(
        hide_pin_locations_in_trips=True
    )
    Profile.objects.filter(trip_pin_location_visibility="anyone").update(
        hide_pin_locations_in_trips=False
    )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0094_sitesettings_ai_controls"),
    ]

    operations = [
        # 1. Add the new field with default 'anyone' so every existing row is safe.
        migrations.AddField(
            model_name="profile",
            name="trip_pin_location_visibility",
            field=models.CharField(
                choices=[
                    ("anyone", "Anyone"),
                    ("friends", "Friends Only"),
                    ("common_pin", "Users with a pin in common"),
                    ("common_friend", "Users with a friend in common"),
                    ("common_trip", "Users with a trip in common"),
                    ("no_one", "No one"),
                ],
                default="anyone",
                max_length=20,
                help_text=(
                    "When you share one of your pins as a trip activity, who can see the "
                    "actual location? Members outside this setting will only see the pin name."
                ),
            ),
        ),
        # 2. Copy data from the old boolean field.
        migrations.RunPython(_migrate_forward, _migrate_reverse),
        # 3. Drop the old boolean field.
        migrations.RemoveField(
            model_name="profile",
            name="hide_pin_locations_in_trips",
        ),
    ]
