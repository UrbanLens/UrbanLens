"""Add FriendInvitation model for email-based friend invites."""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0096_pin_vulnerability"),
    ]

    operations = [
        migrations.CreateModel(
            name="FriendInvitation",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("email", models.EmailField(db_index=True, max_length=254)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "inviter",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sent_invitations",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
