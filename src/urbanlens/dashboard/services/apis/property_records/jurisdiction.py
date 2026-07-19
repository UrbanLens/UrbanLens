"""Resolves a coordinate or address to its ``PropertyJurisdiction`` registry row.

Implements ``docs/property-records-plan.md`` section 1 steps 1-2. Two entry
points, matching the two things a caller usually has on hand:

* :func:`resolve_jurisdiction` - coordinates (the common case: every pin
  already has these). Uses ``CensusTigerwebGateway``, which already exists in
  this codebase for the "US Census" regional-data tab and returns a county
  GEOID directly from a point-in-polygon query - no need for a second
  geocoding round-trip.
* :func:`resolve_jurisdiction_from_address` - a free-text address with no
  known coordinates yet. Uses the new ``CensusGeocoderGateway``, which
  resolves both at once.

Either path ends the same way: :meth:`PropertyJurisdiction.objects.get_or_create_for_fips`
gets-or-creates the registry row for that FIPS code. A freshly-created row
starts at ``AdapterType.UNKNOWN`` - resolution only identifies *which*
jurisdiction a coordinate belongs to, never invents a retrieval strategy for
one nobody has researched yet (see the model's own docstring).
"""

from __future__ import annotations

import logging

from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
from urbanlens.dashboard.services.geo_filter import is_usa_coordinates

logger = logging.getLogger(__name__)


def resolve_jurisdiction(latitude: float, longitude: float) -> PropertyJurisdiction | None:
    """Resolve a coordinate to its county ``PropertyJurisdiction`` registry row.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        The (possibly freshly-created, ``UNKNOWN``-adapter) registry row, or
        None when the coordinate isn't in the USA or TIGERweb has no county
        geography for it (e.g. open ocean).
    """
    if not is_usa_coordinates(latitude, longitude):
        return None

    from urbanlens.dashboard.services.apis.locations.census_tigerweb import CensusTigerwebGateway

    geography = CensusTigerwebGateway().get_geography(latitude, longitude)
    county = geography.get("county")
    state = geography.get("state")
    if not county or not county.get("geoid"):
        logger.debug("No TIGERweb county geography for coordinate; can't resolve a property jurisdiction")
        return None

    fips = str(county["geoid"])
    state_abbr = _state_name_to_abbr(state.get("name")) if state else ""
    jurisdiction, _created = PropertyJurisdiction.objects.get_or_create_for_fips(fips, county_name=county.get("name") or "", state=state_abbr)
    return jurisdiction


def resolve_jurisdiction_from_address(address: str) -> tuple[PropertyJurisdiction, float, float] | None:
    """Resolve a free-text US address to its jurisdiction and matched coordinates.

    Args:
        address: A one-line US street address.

    Returns:
        ``(jurisdiction, latitude, longitude)``, or None when the address
        didn't geocode to a US county.
    """
    from urbanlens.dashboard.services.apis.locations.census_geocoder import CensusGeocoderGateway

    result = CensusGeocoderGateway().geocode_address(address)
    if result is None or not result.fips:
        return None

    jurisdiction, _created = PropertyJurisdiction.objects.get_or_create_for_fips(result.fips, county_name=result.county_name, state=_state_name_to_abbr(result.state_name))
    return jurisdiction, result.latitude, result.longitude


#: Full state/territory name -> USPS abbreviation, for TIGERweb's plain-English
#: state names (Census geocoder results carry the abbreviation already via
#: STATE's numeric FIPS - this table only serves the coordinate path).
_STATE_ABBREVIATIONS: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "Guam": "GU", "American Samoa": "AS", "United States Virgin Islands": "VI",
    "Commonwealth of the Northern Mariana Islands": "MP",
}  # fmt: skip


def _state_name_to_abbr(name: str | None) -> str:
    """Best-effort full state name -> USPS abbreviation; empty string if unrecognized."""
    if not name:
        return ""
    return _STATE_ABBREVIATIONS.get(name.strip(), "")


_STATE_NAMES: dict[str, str] = {abbr: name for name, abbr in _STATE_ABBREVIATIONS.items()}


def state_abbr_to_name(abbr: str | None) -> str:
    """Best-effort USPS state abbreviation -> full name; empty string if unrecognized.

    The inverse of :func:`_state_name_to_abbr`'s table - used by
    ``discovery.discover_via_portal_search``, whose upstream search index
    (ArcGIS Online's own item-search API) matches noticeably worse against a
    bare 2-letter state abbreviation than the spelled-out name: confirmed
    live, ``"Athens County OH parcels"`` returns zero results while
    ``"Athens County Ohio parcels"`` returns real hits for the same county.
    """
    if not abbr:
        return ""
    return _STATE_NAMES.get(abbr.strip().upper(), "")
