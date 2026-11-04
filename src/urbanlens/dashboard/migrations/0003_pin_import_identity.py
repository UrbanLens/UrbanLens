"""Record which importer made a pin and its content-derived key, so a re-import never needs a proximity snap to stay idempotent. The backfill is the next migration, so these locks commit first."""

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0002_describe_seeded_labels")]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="import_source",
            field=models.CharField(blank=True, choices=[("google_takeout", "Google Takeout"), ("json_archive", "UrbanLens JSON archive")], max_length=20, null=True),
        ),
        migrations.AddField(model_name="pin", name="import_key", field=models.CharField(blank=True, max_length=255, null=True)),
        migrations.AddConstraint(
            model_name="pin",
            constraint=models.UniqueConstraint(condition=Q(import_key__isnull=False), fields=("profile", "import_source", "import_key"), name="db_pin_unique_import_key_per_profile"),
        ),
    ]
