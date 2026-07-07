"""Location model - shared, immutable address/coordinate record for a place."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Index
from django.db.models.fields import CharField, SlugField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.location.queryset import LocationManager

if TYPE_CHECKING:
    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.trips.model import TripActivity
    from urbanlens.dashboard.models.wiki.model import Wiki


logger = logging.getLogger(__name__)


class Location(abstract.PublicDashboardModel, abstract.AddressableModel):
    """Shared, immutable address/coordinate record for a physical place.

    Location is the *address* third of the place model:
    - Location  - one row per real-world address, shared and deduplicated by
      coordinates. Treated as immutable: when a pin's or wiki's coordinates
      change we find-or-create a *different* Location instead of mutating it.
    - Wiki      - one community page per Location (1:1); everything users edit
      collectively (name, description, security, badges, aliases, ...).
    - Pin       - one row per (user, place) pair; a user's personal record.

    A Location stores only what is derived from the address itself: coordinates,
    street components (via AddressableMixin), the linked GooglePlace, an
    external-source ``official_name``, and the cache of address-keyed external
    API results (``external_cache``).

    What does NOT belong here (all on Wiki now):
    - Community name / description -> Wiki.name / Wiki.description
    - Security indicators, badges, dates -> Wiki
    - Aliases, comments, edit history, photos -> Wiki
    A user's personal label/notes/visit history belong on Pin.
    """

    # Stable URL routing token (each place resolves its wiki via this slug).
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)

    # External-source name for this place (e.g. from Google). User edits never
    # write this field; the community-editable name lives on Wiki.name.
    official_name = CharField(max_length=255, null=True, blank=True)

    if TYPE_CHECKING:
        wiki: Wiki
        activities: DjangoManager[TripActivity]
        markup_items: DjangoManager[PinMarkup]

    objects = LocationManager()

    @property
    def display_name(self) -> str:
        """Best human-readable name: the community wiki name, else the official name.

        Reads the linked Wiki when present (prefetch with
        ``select_related("wiki")`` in bulk to avoid an extra query per row).
        """
        try:
            wiki = self.wiki
        except ObjectDoesNotExist:
            wiki = None
        if wiki is not None and wiki.name:
            return wiki.name
        return self.official_name or "Unnamed Location"

    def __str__(self):
        return self.official_name or f"Location({self.pk})"

    def to_json(self) -> dict:
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            "id": self.id,
            "name": self.display_name,
            "official_name": self.official_name,
            "place_name": self.place_name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    def _slugify_base(self) -> str:
        # The community-facing name lives on Wiki; a Location slug is only a
        # stable URL routing token, so fall back to the uuid to stay unique
        # and avoid churn when many locations share a blank official_name.
        return self.official_name or str(self.uuid)

    def save(self, *args, **kwargs) -> None:
        """Auto-generate a routing slug and sync the PostGIS point before saving."""
        if not self.slug:
            self.slug = self._generate_slug()
        if self.latitude is not None and self.longitude is not None:
            lon = float(self.longitude)
            lat = float(self.latitude)
            self.point = Point(lon, lat, srid=4326)
        super().save(*args, **kwargs)

    class Meta(abstract.PublicDashboardModel.Meta, abstract.AddressableModel.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_loc_uuid"),
            Index(fields=["latitude", "longitude"], name="idxdb_loc_lat_long"),
            Index(fields=["official_name"], name="idxdb_loc_offname"),
            Index(fields=["google_place"], name="idxdb_loc_gplace"),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
