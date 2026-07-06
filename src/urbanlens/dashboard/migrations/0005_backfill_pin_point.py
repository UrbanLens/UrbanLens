"""Backfill Pin.point from effective coordinates (own override, else Location's)."""

from __future__ import annotations

from django.contrib.gis.geos import Point
from django.db import migrations


def backfill_pin_point(apps, schema_editor):
    Pin = apps.get_model("dashboard", "Pin")
    pins = Pin.objects.select_related("location").only(
        "latitude", "longitude", "point", "location__latitude", "location__longitude"
    )
    updated = []
    for pin in pins.iterator():
        latitude = pin.latitude if pin.latitude is not None else (pin.location.latitude if pin.location else None)
        longitude = pin.longitude if pin.longitude is not None else (pin.location.longitude if pin.location else None)
        if latitude is None or longitude is None:
            continue
        point = Point(float(longitude), float(latitude), srid=4326)
        if pin.point is None or (pin.point.x, pin.point.y) != (point.x, point.y):
            pin.point = point
            updated.append(pin)

    if updated:
        Pin.objects.bulk_update(updated, ["point"], batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0004_safetycheckin_slug"),
    ]

    operations = [
        migrations.RunPython(backfill_pin_point, noop),
    ]
