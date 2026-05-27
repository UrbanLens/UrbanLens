"""Add remove_trip_members permission to TripMembership."""

from __future__ import annotations

from django.db import migrations


class Migration(migrations.Migration):
    """Add remove_trip_members custom permission on TripMembership."""

    dependencies = [
        ("dashboard", "0049_site_admin_permission"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="tripmembership",
            options={
                "permissions": [("remove_trip_members", "Can remove members from trips")],
            },
        ),
    ]
