"""Give NotificationLog a public uuid.

Three steps rather than one: a unique column cannot be added to a table that
already has rows without first giving every existing row its own value. The
column arrives nullable, is backfilled row by row, and only then becomes
non-null with its callable default.

**The AddField must not carry ``default=uuid.uuid4``.** Django evaluates a
callable default *once* per ``AddField`` and writes that single value into
every pre-existing row, so a table with more than one notification would end
up with one shared uuid: the backfill below then matches nothing (no row is
null), and the final ``AlterField`` fails on the duplicates - a migration that
passes on an empty dev database and blocks deployment on a real one. The
nullable column starts with no default at all, which is what makes the
backfill the thing that assigns values.

``unique=True`` rides along on the ``AddField`` rather than arriving with the
final ``AlterField``: nulls do not collide in Postgres, and adding the
constraint later would build a second index over the same column (see
``migrations/CLAUDE.md``).

The uuid exists because the external API addresses notifications by it - the
sequential pk would otherwise become the public handle, leaking notification
volume and letting a caller walk neighbouring ids.
"""

from __future__ import annotations

import uuid

from django.db import migrations, models


def _populate_uuids(apps, schema_editor) -> None:
    """Give every pre-existing notification its own uuid.

    Args:
        apps: The historical app registry.
        schema_editor: The active schema editor (unused).
    """
    notification_log = apps.get_model("dashboard", "NotificationLog")
    rows = notification_log.objects.filter(uuid__isnull=True).only("id")
    batch: list = []
    for row in rows.iterator(chunk_size=2_000):
        row.uuid = uuid.uuid4()
        batch.append(row)
        if len(batch) >= 2_000:
            notification_log.objects.bulk_update(batch, ["uuid"])
            batch = []
    if batch:
        notification_log.objects.bulk_update(batch, ["uuid"])


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0014_message_client_uuid"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="uuid",
            field=models.UUIDField(editable=False, null=True, unique=True),
        ),
        migrations.RunPython(_populate_uuids, migrations.RunPython.noop, elidable=True),
        migrations.AlterField(
            model_name="notificationlog",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
    ]
