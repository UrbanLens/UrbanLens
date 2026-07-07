"""Add PinVisit.map_data and Image.visit so visits can carry photos and markup."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0001_initial_squashed_0006_alter_notificationlog_notification_type_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinvisit",
            name="map_data",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="visit",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="images",
                to="dashboard.pinvisit",
            ),
        ),
    ]
