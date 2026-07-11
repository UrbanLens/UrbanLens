"""GeocodedLocation model - cached geocoding API responses."""

from __future__ import annotations

from django.db.models import Index
from django.db.models.fields import CharField, DecimalField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.cache.queryset import GeocodedLocationManager


class GeocodedLocation(abstract.DashboardModel):
    """Records geocoded location data."""

    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    place_name = CharField(max_length=255, null=True, blank=True)
    json_response = CharField(max_length=50000, null=True, blank=True)

    objects = GeocodedLocationManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_geocoded_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["latitude", "longitude"], name="idxdb_geoloc_lat_lng"),
            Index(fields=["place_name"], name="idxdb_geoloc_placename"),
        ]
