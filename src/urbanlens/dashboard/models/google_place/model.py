"""GooglePlace model - cached Google Place metadata keyed by coordinates."""

from __future__ import annotations

from django.db.models import Index, UniqueConstraint
from django.db.models.fields import CharField, DecimalField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.google_place.queryset import GooglePlaceManager


class GooglePlace(abstract.DashboardModel):
    """Cached Google Place / geocoding metadata for a coordinate pair.

    Deduplicated by (latitude, longitude): any two Location rows that share
    coordinates reference the same GooglePlace row, so Google's APIs are only
    contacted once per point. A Pin has no ``google_place`` FK of its own - a
    pin's coordinates are always its linked Location's, so it always reaches
    this cache via ``pin.location.google_place``; a detail/child pin whose own
    Location has different coordinates than its parent's simply points at a
    different Location, which gets its own GooglePlace row (or shares one, if
    those coordinates happen to already have a cached row).
    """

    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)
    cached_place_name = CharField(max_length=255, null=True, blank=True)
    # Google Maps CID - unsigned 64-bit identifier embedded in place URLs.
    cid = DecimalField(max_digits=20, decimal_places=0, null=True, blank=True, unique=True)
    place_id = CharField(max_length=255, null=True, blank=True)

    objects = GooglePlaceManager()

    def __str__(self) -> str:
        label = self.cached_place_name or f"({self.latitude}, {self.longitude})"
        return f"GooglePlace: {label}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_google_places"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["latitude", "longitude"], name="idxdb_gplace_lat_long"),
            Index(fields=["cid"], name="idxdb_gplace_cid"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["latitude", "longitude"],
                name="db_google_place_unique_coordinates",
            ),
        ]
