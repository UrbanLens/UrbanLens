"""Street-address backfill for Locations that only have coordinates."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import DatabaseError

from urbanlens.dashboard.services.rate_limiter import RequestCancelledError

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def ensure_location_address(location: Location | None) -> bool:
    """Populate address fields on a Location that has coordinates but no street data.

    Calls the Google Geocoding API (with GeocodedLocation as an intermediate cache),
    then writes the parsed components back to the Location row so the next request
    reads directly from the DB with no API call. Used lazily by the pin overview
    page and proactively by background enrichment
    (:class:`~urbanlens.dashboard.services.enrichment.AddressEnrichmentSource`).

    Args:
        location: The location to backfill; no-ops when None or already addressed.

    Returns:
        True when at least one address component was written.
    """
    if not location or location.route:
        return False
    lat = float(location.latitude) if location.latitude is not None else None
    lng = float(location.longitude) if location.longitude is not None else None
    if lat is None or lng is None:
        return False

    try:
        from urbanlens.dashboard.services.apis.locations.google.geocoding import GoogleGeocodingGateway, parse_address_components
        from urbanlens.UrbanLens.settings.app import settings as app_settings

        if not app_settings.google_unrestricted_api_key:
            return False

        data = GoogleGeocodingGateway().geocode_coordinates(lat, lng)
        if not data:
            return False
        results = data.get("results", [])
        if not results:
            return False

        type_map = parse_address_components(results[0].get("address_components", []))

        update_fields: list[str] = []

        def _maybe_set(field: str, value: str | None) -> None:
            if value and not getattr(location, field):
                setattr(location, field, value)
                update_fields.append(field)

        _maybe_set("street_number", type_map.get("street_number"))
        _maybe_set("route", type_map.get("route"))
        _maybe_set("locality", type_map.get("locality"))
        _maybe_set("administrative_area_level_1", type_map.get("administrative_area_level_1"))
        _maybe_set("administrative_area_level_2", type_map.get("administrative_area_level_2"))
        _maybe_set("zipcode", type_map.get("postal_code"))
        _maybe_set("country", type_map.get("country"))

        if update_fields:
            location.save(update_fields=update_fields)
        return bool(update_fields)
    except (ImportError, OSError, ValueError, DatabaseError, RequestCancelledError):
        logger.exception("Reverse geocoding failed for location pk=%s", getattr(location, "pk", None))
        return False
