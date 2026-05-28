"""Remap profile_visibility and comment_visibility to unified VisibilityChoice values.

Old values: everyone → anyone, only_me → no_one, common_locations → common_pin
Then AlterField both columns to the full VisibilityChoice set.
"""

from django.db import migrations, models


def _remap_visibility(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    mapping = {
        "everyone": "anyone",
        "only_me": "no_one",
        "common_locations": "common_pin",
    }
    for old, new in mapping.items():
        Profile.objects.filter(profile_visibility=old).update(profile_visibility=new)
        Profile.objects.filter(comment_visibility=old).update(comment_visibility=new)


_NEW_CHOICES = [
    ("anyone", "Anyone"),
    ("friends", "Friends Only"),
    ("common_pin", "Users with a pin in common"),
    ("common_friend", "Users with a friend in common"),
    ("common_trip", "Users with a trip in common"),
    ("no_one", "No one"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0060_alter_profile_friend_request_visibility"),
    ]

    operations = [
        migrations.RunPython(_remap_visibility, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="profile",
            name="profile_visibility",
            field=models.CharField(
                choices=_NEW_CHOICES,
                default="anyone",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="comment_visibility",
            field=models.CharField(
                choices=_NEW_CHOICES,
                default="anyone",
                max_length=20,
            ),
        ),
    ]
