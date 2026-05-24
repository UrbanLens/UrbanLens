import uuid
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0021_location_wiki"),
    ]

    operations = [
        # ── Location.uuid ───────────────────────────────────────────────────
        migrations.AddField(
            model_name="location",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
        migrations.AddIndex(
            model_name="location",
            index=models.Index(fields=["uuid"], name="dashboard_loc_uuid_idx"),
        ),
        # ── Pin.uuid ────────────────────────────────────────────────────────
        migrations.AddField(
            model_name="pin",
            name="uuid",
            field=models.UUIDField(default=uuid.uuid4, unique=True, editable=False),
        ),
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(fields=["uuid"], name="dashboard_pin_uuid_idx"),
        ),
    ]
