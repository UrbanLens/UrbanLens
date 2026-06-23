from __future__ import annotations

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add instance_uuid to SiteSettings and a (profile, updated) index to Pin.

    instance_uuid: auto-generated per DB instance; clients embed it in their local
    pin cache so a mismatch (DB wipe / redeploy) triggers a cache clear.

    (profile, updated) index: covers the MAX(updated) aggregate used by the cache
    invalidation polling endpoint.
    """

    dependencies = [
        ("dashboard", "0002_pin_is_private"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="instance_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["profile", "updated"], name="dashboard_pin_profile_updated_idx"),
        ),
    ]
