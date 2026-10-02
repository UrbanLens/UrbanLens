from django.contrib.gis.geos import Point
from django.db import migrations
from django.utils import timezone

from urbanlens.dashboard.models.place.model import PlaceKind, PlaceStatus, implausible_area_q
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE


def _reresolve_fiat_building_places(apps, schema_editor):
    """P181: put each Location the pre-0.8.0 sweep attached to an outline-less building back where containment puts it.

    Containment is ``Place.objects.resolve_for_point``'s, restated against historical models. A Location left on no place
    is unstamped, as ``detach_oversized_place`` does, so the provider chain is asked about it again.
    """
    Location = apps.get_model("dashboard", "Location")
    Place = apps.get_model("dashboard", "Place")
    LocationCache = apps.get_model("dashboard", "LocationCache")

    implausible = Place.objects.filter(implausible_area_q() | implausible_area_q("domain_root__"))
    resolvable = Place.objects.filter(status=PlaceStatus.CURRENT, is_aggregate=False, geometry__isnull=False).exclude(pk__in=implausible.values("pk"))
    stale = Location.objects.filter(place__kind=PlaceKind.BUILDING, place__geometry__isnull=True).order_by("pk")
    for location in stale.iterator():
        point = Point(float(location.longitude), float(location.latitude), srid=4326)
        place = resolvable.filter(geometry__contains=point).order_by("area_sqm", "pk").first()
        Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=timezone.now() if place is not None else None)
        LocationCache.objects.filter(location_id=location.pk, source=PARCEL_BUILDINGS_CACHE_SOURCE).delete()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0033_v0_8_0_indexes")]
    operations = [
        # One-way: no current path attaches a Location to a place without an outline.
        migrations.RunPython(_reresolve_fiat_building_places, migrations.RunPython.noop),
    ]
