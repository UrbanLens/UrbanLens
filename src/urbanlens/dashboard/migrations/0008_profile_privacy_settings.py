"""Add privacy/visibility settings fields to Profile."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0007_profile_social_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="profile_visibility",
            field=models.CharField(
                choices=[
                    ("only_me", "Only Me"),
                    ("friends", "Friends Only"),
                    ("common_locations", "People with Common Locations"),
                    ("everyone", "Everyone"),
                ],
                default="everyone",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="comment_visibility",
            field=models.CharField(
                choices=[
                    ("only_me", "Only Me"),
                    ("friends", "Friends Only"),
                    ("common_locations", "People with Common Locations"),
                    ("everyone", "Everyone"),
                ],
                default="everyone",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="allow_friend_requests",
            field=models.BooleanField(default=True),
        ),
    ]
