from django.db import migrations, transaction
from django.db.models import F


CHUNK = 500


def backfill_lifecycle(apps, schema_editor):
    """One initial geometry version per place with an outline, and validity from what we already know, one transaction per primary-key chunk."""
    Place = apps.get_model("dashboard", "Place")
    Version = apps.get_model("dashboard", "PlaceGeometryVersion")
    last = 0
    while True:
        chunk = list(Place.objects.filter(pk__gt=last).order_by("pk").values_list("pk", "geometry", "geometry_generated_at", "created")[:CHUNK])
        if not chunk:
            return
        last = chunk[-1][0]
        pks = [row[0] for row in chunk]
        with transaction.atomic():
            has_version = set(Version.objects.filter(place_id__in=pks).values_list("place_id", flat=True))
            Version.objects.bulk_create(
                [Version(place_id=pk, geometry=geometry, recorded_at=generated_at or created, provider="") for pk, geometry, generated_at, created in chunk if geometry is not None and pk not in has_version],
            )
            Place.objects.filter(pk__in=pks, valid_from__isnull=True).update(valid_from=F("created"))
            Place.objects.filter(pk__in=pks, status="superseded", valid_to__isnull=True).update(valid_to=F("updated"))


def unbackfill_lifecycle(apps, schema_editor):
    """The tables are dropped with the migration; nothing to undo."""


class Migration(migrations.Migration):
    """Data-only, so a failed backfill can be retried without redoing the 0004 schema changes."""

    atomic = False

    dependencies = [
        ("dashboard", "0004_place_lifecycle"),
    ]

    operations = [
        migrations.RunPython(backfill_lifecycle, unbackfill_lifecycle),
    ]
