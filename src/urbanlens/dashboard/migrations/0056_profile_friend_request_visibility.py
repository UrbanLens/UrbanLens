"""Replace boolean allow_friend_requests with FriendRequestVisibility choice field."""
from django.db import migrations, models


def migrate_friend_requests(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.filter(allow_friend_requests=False).update(friend_request_visibility="no_one")


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0055_alter_comment_options_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="friend_request_visibility",
            field=models.CharField(
                choices=[
                    ("anyone", "From anyone"),
                    ("common_pin", "From users with a pin in common"),
                    ("common_friend", "From users with a friend in common"),
                    ("common_trip", "From users with a trip in common"),
                    ("no_one", "No one"),
                ],
                default="anyone",
                max_length=20,
            ),
        ),
        migrations.RunPython(migrate_friend_requests, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="profile",
            name="allow_friend_requests",
        ),
    ]
