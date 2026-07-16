"""Backfill PinShare.location snapshots and LocationExposure rows for existing shares.

Existing shares predate location snapshotting ("track both the pin and the
location"), so:

1. every share with a pin but no ``location`` gets the pin's *current*
   location - the best available approximation of where the pin was when it
   was shared;
2. every share whose recipient didn't already have their own pin at that
   location *before the share arrived* gets a ``LocationExposure``, so the
   gaming-proof chain resolution covers pre-existing history too. Recipients
   who pinned the place first (their pin's ``created`` predates the share,
   and it wasn't itself created from a share) already knew it - no exposure.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    """Populate location snapshots and exposures from existing share history."""
    PinShare = apps.get_model("dashboard", "PinShare")
    LocationExposure = apps.get_model("dashboard", "LocationExposure")
    Pin = apps.get_model("dashboard", "Pin")

    for share in PinShare.objects.filter(location__isnull=True, pin__isnull=False).select_related("pin"):
        PinShare.objects.filter(pk=share.pk, location__isnull=True).update(location_id=share.pin.location_id)

    for share in PinShare.objects.filter(location__isnull=False).select_related("location"):
        prior_own_pin = Pin.objects.filter(
            profile_id=share.to_profile_id,
            parent_pin__isnull=True,
            location_id=share.location_id,
            created__lt=share.created,
            source_share__isnull=True,
        ).exists()
        if prior_own_pin:
            continue
        LocationExposure.objects.get_or_create(
            profile_id=share.to_profile_id,
            location_id=share.location_id,
            share_id=share.pk,
            defaults={"source": "share_received"},
        )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0055_directmessagelocationmention_locationexposure_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
