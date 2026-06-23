from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add is_private flag to Pin.

    Private pins are never linked to a shared Location and do not contribute to
    the community wiki.  The field defaults to False so all existing pins retain
    their current public behaviour.
    """

    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="is_private",
            field=models.BooleanField(default=False),
        ),
    ]
