"""Street-address backfill for Locations that only have coordinates."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from django.db import DatabaseError

from urbanlens.dashboard.services.core.gateway import is_source_outage
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

if TYPE_CHECKING:
    from collections.abc import Callable

    from urbanlens.dashboard.models.location.model import Location

logger = logging.getLogger(__name__)


def ensure_location_address(location: Location | None) -> bool:
    """Populate address fields on a Location that has coordinates but no street data.
    Calls the Google Geocoding API (with GeocodedLocation as an intermediate cache), then writes the parsed components back to the Location row so the next request reads directly from the DB with no API call.

    Args:
        location: The location to backfill; no-ops when None or already addressed.

    Returns:
        True when at least one address component was written.

    Raises:
        Exception: Google did not supply the address and a provider could not be asked (see ``is_source_outage``);
            whatever OpenStreetMap wrote is kept.
    """
    if not location or location.route:
        return False
    lat = float(location.latitude) if location.latitude is not None else None
    lng = float(location.longitude) if location.longitude is not None else None
    if lat is None or lng is None:
        return False

    outages: list[Exception] = []
    written = False
    backfills: tuple[Callable[[Location, float, float], bool], ...] = (_google_address, _openstreetmap_admin_address)
    for backfill in backfills:
        try:
            written = backfill(location, lat, lng)
        except Exception as exc:
            if not is_source_outage(exc):
                raise
            outages.append(exc)
        if written:
            break
    if outages:
        raise outages[0]
    return written


#: OpenStreetMap names some municipalities by their form of government ("Town of Poughkeepsie"); addresses use the bare name.
_MUNICIPAL_PREFIX = re.compile(r"^(?:town|city|village|township|borough|municipality)\s+of\s+", re.IGNORECASE)


def _openstreetmap_admin_address(location: Location, lat: float, lng: float) -> bool:
    """Fill the administrative address fields from Nominatim when Google could not.

    Never the street: the nearest OSM way is as likely a campus service road as the postal street, and the
    street feeds every search query.

    Args:
        location: The location to backfill.
        lat: Its latitude.
        lng: Its longitude.

    Returns:
        True when at least one field was written.

    Raises:
        Exception: Nominatim could not be asked (see ``is_source_outage``).
    """
    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

    try:
        admin = NominatimGateway().reverse_geocode_admin(lat, lng)
    except Exception as exc:
        if is_source_outage(exc):
            raise
        logger.warning("OpenStreetMap address fallback failed for location pk=%s", getattr(location, "pk", None), exc_info=True)
        return False
    if not admin:
        return False
    values = {
        "locality": _MUNICIPAL_PREFIX.sub("", admin.get("city") or "").strip(),
        "administrative_area_level_1": admin.get("state") or "",
        "administrative_area_level_2": admin.get("county") or "",
        "zipcode": (admin.get("postcode") or "")[:10],
        "country": admin.get("country") or "",
    }
    update_fields = [field for field, value in values.items() if value and not getattr(location, field)]
    for field in update_fields:
        setattr(location, field, values[field])
    if update_fields:
        location.save(update_fields=update_fields)
    return bool(update_fields)


def _google_address(location: Location, lat: float, lng: float) -> bool:
    """Fill the address from Google's reverse geocode; False when there is no key or no answer.

    Raises:
        Exception: Google could not be asked (see ``is_source_outage``).
    """
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
    except (ImportError, OSError, ValueError, DatabaseError, RequestCancelledError) as exc:
        if is_source_outage(exc):
            raise
        logger.exception("Reverse geocoding failed for location pk=%s", getattr(location, "pk", None))
        return False
