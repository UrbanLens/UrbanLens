"""Persist signup invite tokens on email verification records."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0023_pin_sharing"),
    ]

    operations = [
        migrations.AddField(
            model_name="emailverification",
            name="pending_invite_token",
            field=models.UUIDField(blank=True, null=True),
        ),
    ]
