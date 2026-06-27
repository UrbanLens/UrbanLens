"""Shared Google Place metadata keyed by coordinates."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction

from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.services.google.geocoding import GoogleGeocodingGateway
from urbanlens.dashboard.services.locations.google import PlaceNameResolverChain
from urbanlens.dashboard.services.locations.naming import is_meaningful_name
from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from urbanlens.dashboard.models.abstract.addressable import AddressableModel

logger = logging.getLogger(__name__)

_COORD_QUANT = Decimal("0.000001")


def normalize_coordinate(value: float | Decimal) -> Decimal:
    """Normalize a coordinate to six decimal places for stable lookups.

    Args:
        value: Raw latitude or longitude.

    Returns:
        Quantized decimal suitable for database equality checks.
    """
    return Decimal(str(value)).quantize(_COORD_QUANT, rounding=ROUND_HALF_UP)


@dataclass
class GooglePlaceService:
    """Resolve, cache, and link Google Place data for coordinates."""

    name_resolver: PlaceNameResolverChain = field(default_factory=PlaceNameResolverChain)

    def get_for_coordinates(
        self,
        latitude: float | Decimal,
        longitude: float | Decimal,
    ) -> GooglePlace | None:
        """Return an existing GooglePlace row for coordinates, if any.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            Matching GooglePlace or None.
        """
        lat = normalize_coordinate(latitude)
        lon = normalize_coordinate(longitude)
        return GooglePlace.objects.filter(latitude=lat, longitude=lon).first()

    def get_or_create_for_coordinates(
        self,
        latitude: float | Decimal,
        longitude: float | Decimal,
        *,
        place_name: str | None = None,
        cid: int | Decimal | None = None,
        fetch_if_missing: bool = True,
    ) -> GooglePlace:
        """Return the shared GooglePlace row for a coordinate pair.

        Creates the row and optionally contacts Google when no cached data exists.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            place_name: Optional pre-resolved place name to store.
            cid: Optional Google Maps CID to store.
            fetch_if_missing: When True, call Google if no cached name is available.

        Returns:
            The existing or newly created GooglePlace instance.
        """
        lat = normalize_coordinate(latitude)
        lon = normalize_coordinate(longitude)
        existing = self.get_for_coordinates(lat, lon)
        if existing is not None:
            return self._merge_into_existing(existing, place_name=place_name, cid=cid, fetch_if_missing=fetch_if_missing)

        resolved_name = place_name
        if fetch_if_missing and not is_meaningful_name(resolved_name):
            resolved_name = self._resolve_name(float(lat), float(lon))

        try:
            with transaction.atomic():
                return GooglePlace.objects.create(
                    latitude=lat,
                    longitude=lon,
                    cached_place_name=resolved_name,
                    cid=cid,
                )
        except IntegrityError:
            existing = GooglePlace.objects.get(latitude=lat, longitude=lon)
            return self._merge_into_existing(existing, place_name=place_name, cid=cid, fetch_if_missing=fetch_if_missing)

    def ensure_linked(self, entity: AddressableModel) -> GooglePlace | None:
        """Attach entity.google_place to the shared row for its coordinates.

        Args:
            entity: A Location or Pin with latitude and longitude set.

        Returns:
            Linked GooglePlace, or None when coordinates are invalid.
        """
        if entity.latitude is None or entity.longitude is None:
            return None
        google_place = self.get_or_create_for_coordinates(entity.latitude, entity.longitude)
        if entity.google_place_id != google_place.pk:
            entity.__class__.objects.filter(pk=entity.pk).update(google_place_id=google_place.pk)
            entity.google_place_id = google_place.pk
            entity.google_place = google_place
        return google_place

    def set_cid_for_entity(self, entity: AddressableModel, cid: int | Decimal) -> GooglePlace:
        """Store a Google Maps CID on the shared row for an entity's coordinates.

        Args:
            entity: Location or Pin whose coordinates identify the cache row.
            cid: Google Maps CID extracted from an import URL.

        Returns:
            The GooglePlace row that now holds the CID.
        """
        google_place = self.get_or_create_for_coordinates(
            entity.latitude,
            entity.longitude,
            cid=cid,
            fetch_if_missing=False,
        )
        self.ensure_linked(entity)
        if google_place.cid is None:
            GooglePlace.objects.filter(pk=google_place.pk, cid__isnull=True).update(cid=cid)
            google_place.cid = cid
        return google_place

    def resolve_place_name(self, google_place: GooglePlace) -> str:
        """Return a cached or freshly fetched place name for a GooglePlace row.

        Args:
            google_place: Row to read or populate.

        Returns:
            Resolved place name, or the sentinel ``No Information Available``.
        """
        if google_place.cached_place_name:
            return google_place.cached_place_name
        name = self._resolve_name(float(google_place.latitude), float(google_place.longitude))
        if not name:
            name = "No Information Available"
        GooglePlace.objects.filter(pk=google_place.pk).update(cached_place_name=name)
        google_place.cached_place_name = name
        return name

    def _merge_into_existing(
        self,
        google_place: GooglePlace,
        *,
        place_name: str | None,
        cid: int | Decimal | None,
        fetch_if_missing: bool,
    ) -> GooglePlace:
        updates: dict[str, object] = {}
        if place_name and not google_place.cached_place_name:
            updates["cached_place_name"] = place_name
        if cid and google_place.cid is None:
            updates["cid"] = cid
        if updates:
            GooglePlace.objects.filter(pk=google_place.pk).update(**updates)
            google_place.refresh_from_db()
        elif fetch_if_missing and not google_place.cached_place_name:
            self.resolve_place_name(google_place)
        return google_place

    def _resolve_name(self, latitude: float, longitude: float) -> str | None:
        name = self.name_resolver.resolve(latitude, longitude)
        if is_meaningful_name(name):
            return name
        try:
            return GoogleGeocodingGateway(api_key=settings.google_maps_api_key).get_place_name(latitude, longitude)
        except (OSError, ValueError) as exc:
            logger.debug("Google place-name lookup failed for %s,%s: %s", latitude, longitude, exc)
            return None
