"""Record which importer made a pin and its content-derived key, so a re-import never needs a proximity snap to stay idempotent."""

from django.db import migrations, models
from django.db.models import Q

CHUNK = 2000


def key_takeout_pins(apps, schema_editor):
    """
    Key existing pins whose Location carries a Google cid, as Takeout pins.
    The cid is the one Takeout key still derivable after the fact. Only root pins are keyed, each cid once per profile, and a pin already keyed is left alone.
    """
    Pin = apps.get_model("dashboard", "Pin")
    last_pk = 0
    seen: set[tuple[int, str]] = set()
    while True:
        rows = list(Pin.objects.filter(pk__gt=last_pk, parent_pin__isnull=True, import_key__isnull=True, location__google_place__cid__isnull=False).order_by("pk").values_list("pk", "profile_id", "location__google_place__cid")[:CHUNK])
        if not rows:
            return
        last_pk = rows[-1][0]
        for pk, profile_id, cid in rows:
            key = f"cid:{int(cid)}"
            if (profile_id, key) in seen:
                continue
            seen.add((profile_id, key))
            Pin.objects.filter(pk=pk, import_key__isnull=True).update(import_source="google_takeout", import_key=key)


def unkey(apps, schema_editor):
    """Reverse of the backfill is the column drop."""


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
        migrations.RunPython(key_takeout_pins, unkey),
    ]
