"""Backfill Location for every Pin that pre-dates the Location requirement.

For each pin whose location FK is NULL and whose latitude/longitude are set,
we either find an existing Location whose bounding box contains the pin's
coordinates, or create a new one.  This is the same logic as post_add_pin.
"""

import uuid

from django.db import migrations


def _backfill(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    Location = apps.get_model("dashboard", "Location")

    try:
        from django.contrib.gis.geos import Point as GEOSPoint
        gis_available = True
    except ImportError:
        gis_available = False

    unlinked = Pin.objects.filter(location__isnull=True, latitude__isnull=False, longitude__isnull=False, parent_pin__isnull=True)

    for pin in unlinked.iterator():
        lat = float(pin.latitude)
        lon = float(pin.longitude)
        location = None

        if gis_available:
            pt = GEOSPoint(lon, lat, srid=4326)
            location = (
                Location.objects.filter(bounding_box__contains=pt).first()
                or Location.objects.filter(bounding_box__isnull=True, latitude=lat, longitude=lon).first()
            )

        if location is None:
            location = Location.objects.create(
                uuid=uuid.uuid4(),
                name=pin.nickname or "Unnamed Location",
                latitude=lat,
                longitude=lon,
            )

        pin.location = location
        pin.save(update_fields=["location"])


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0023_detail_pins"),
    ]

    operations = [
        migrations.RunPython(_backfill, reverse_code=migrations.RunPython.noop),
    ]
