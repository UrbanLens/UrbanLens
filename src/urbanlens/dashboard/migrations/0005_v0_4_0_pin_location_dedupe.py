# v0.4.0 upgrade, part 4 of 5 (inserted after the fact): defensive dedup for
# db_pin_unique_location_per_profile (location, profile) added in what is now
# 0006. 0003's backfill_pin_locations resolves each pin's Location
# independently via get_or_create(latitude, longitude); it cannot see a
# collision that only becomes real once two *different* pins for the same
# profile happen to resolve to the *same* shared Location row (e.g. a former
# location-detail pin sitting at the same coordinates as an unrelated root pin
# for that profile - the two were never required to be distinct pre-upgrade,
# since the old constraint exempted detail pins). Mirrors dedupe_wikis_per_location
# in 0003, but for pins, and has to be its own migration because 0002-0004
# were already applied on production before this collision surfaced.
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.db import migrations

COORD_STEP = Decimal("0.000001")


def _quantize_coord(value) -> Decimal:
    return Decimal(str(value)).quantize(COORD_STEP)


def _point_for(latitude: Decimal, longitude: Decimal) -> Point:
    return Point(float(longitude), float(latitude), srid=4326)


def dedupe_pin_locations(apps, schema_editor):
    """Give any root pin that collides with another root pin of the same
    profile (same Location) a Location of its own, nudged apart from the
    original. Also drops the relocated pin's wiki link, since the wiki (if
    any) belonged to the Location it no longer occupies.
    """
    Pin = apps.get_model("dashboard", "Pin")
    Location = apps.get_model("dashboard", "Location")
    Wiki = apps.get_model("dashboard", "Wiki")

    seen: set[tuple[int, int]] = set()
    pins = Pin.objects.filter(parent_pin__isnull=True).select_related("location").order_by("profile_id", "location_id", "pk")
    for pin in pins.iterator():
        key = (pin.profile_id, pin.location_id)
        if key not in seen:
            seen.add(key)
            continue

        source = pin.location
        latitude = _quantize_coord(source.latitude)
        longitude = _quantize_coord(source.longitude)
        while Location.objects.filter(latitude=latitude, longitude=longitude).exists():
            longitude += COORD_STEP
        new_location = Location.objects.create(
            official_name=source.official_name,
            latitude=latitude,
            longitude=longitude,
            point=_point_for(latitude, longitude),
        )
        pin.location_id = new_location.pk
        pin.wiki_id = Wiki.objects.filter(location_id=new_location.pk).values_list("pk", flat=True).first()
        pin.save(update_fields=["location", "wiki"])
        seen.add((pin.profile_id, new_location.pk))


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_v0_4_0_cleanup"),
    ]

    operations = [
        migrations.RunPython(
            code=dedupe_pin_locations,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
