from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0088_location_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="photo_upload_visibility",
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
                help_text="Who can see the photos you upload to pins and locations.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="viewer_photo_filter",
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
                help_text="Whose photos you want to see. Photos from users outside this setting will be blurred.",
            ),
        ),
    ]
