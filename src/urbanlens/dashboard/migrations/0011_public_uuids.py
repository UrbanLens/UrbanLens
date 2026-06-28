# Generated manually to add stable public UUIDs to exportable user data models.

from __future__ import annotations

import uuid

from django.db import migrations, models


def backfill_public_uuids(apps, schema_editor):
    """Assign a distinct UUID to every row before the unique constraint is added.

    AddField must not use a Python default: PostgreSQL applies one computed
    default to all existing rows, which would violate the later unique index.
    """
    for model_name in ("Badge", "Comment", "Image", "PinVisit"):
        model = apps.get_model("dashboard", model_name)
        seen: set[uuid.UUID] = set()
        for obj in model.objects.all().only("pk", "uuid"):
            if obj.uuid is None or obj.uuid in seen:
                obj.uuid = uuid.uuid4()
                obj.save(update_fields=["uuid"])
            seen.add(obj.uuid)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0010_rename_nickname_pin_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="badge",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="comment",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="image",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.AddField(
            model_name="pinvisit",
            name="uuid",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_public_uuids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="badge",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="comment",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="image",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="pinvisit",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddIndex(
            model_name="badge",
            index=models.Index(fields=["uuid"], name="dashboard_badge_uuid_idx"),
        ),
        migrations.AddIndex(
            model_name="comment",
            index=models.Index(fields=["uuid"], name="dashboard_comment_uuid_idx"),
        ),
        migrations.AddIndex(
            model_name="image",
            index=models.Index(fields=["uuid"], name="dashboard_image_uuid_idx"),
        ),
        migrations.AddIndex(
            model_name="pinvisit",
            index=models.Index(fields=["uuid"], name="dashboard_pv_uuid_idx"),
        ),
    ]
