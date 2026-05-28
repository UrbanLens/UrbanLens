"""Add IGNORED choice to Friendship.status field."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0058_alter_notificationpreference_added_to_trip_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="friendship",
            name="status",
            field=models.CharField(
                max_length=10,
                choices=[
                    ("Requested", "Requested"),
                    ("Accepted", "Accepted"),
                    ("Declined", "Declined"),
                    ("Removed", "Removed"),
                    ("Muted", "Muted"),
                    ("Blocked", "Blocked"),
                    ("Ignored", "Ignored"),
                ],
            ),
        ),
    ]
